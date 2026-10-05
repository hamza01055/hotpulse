"""Fetch sources (RSS/Atom, JSON Feed, Remote OK API) and store new items with de-duplication."""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable

import feedparser
import httpx

from .db import index_item, session, utcnow
from .text import canonical_url, clean_html, detect_script_lang, parse_iso, title_hash, to_iso, squash

log = logging.getLogger("hotpulse.collect")

USER_AGENT = "Mozilla/5.0 (compatible; HotPulse/0.1; +https://github.com/hamza01055/hotpulse)"
Fetcher = Callable[[str], bytes]


def http_fetch(url: str) -> bytes:
    with httpx.Client(timeout=25, follow_redirects=True, headers={"User-Agent": USER_AGENT,
                      "Accept": "application/rss+xml, application/atom+xml, application/json, text/xml, */*"}) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.content


# ---------------------------------------------------------------------------
# Parsers: each returns a list of normalised entries
# ---------------------------------------------------------------------------
def parse_rss(raw: bytes) -> list[dict]:
    feed = feedparser.parse(raw)
    if feed.bozo and not feed.entries:
        raise ValueError(f"not a valid feed: {feed.get('bozo_exception')}")
    out = []
    for e in feed.entries:
        link = e.get("link") or ""
        title = clean_html(e.get("title") or "")
        if not link or not title:
            continue
        body = ""
        if e.get("content"):
            body = max((c.get("value", "") for c in e.content), key=len)
        body = body or e.get("summary") or e.get("description") or ""
        image = None
        for key in ("media_content", "media_thumbnail"):
            if e.get(key):
                image = e[key][0].get("url")
                break
        if not image:
            for enc in e.get("enclosures", []) or []:
                if str(enc.get("type", "")).startswith("image"):
                    image = enc.get("href")
                    break
        out.append({
            "url": link, "title": title, "author": e.get("author"),
            "published": to_iso(e.get("published_parsed") or e.get("updated_parsed") or e.get("published")),
            "content": clean_html(body), "image": image,
        })
    return out


def parse_json_feed(raw: bytes) -> list[dict]:
    """JSON Feed 1.x, or a plain list of {title, url|link, date, description} objects."""
    data = json.loads(raw)
    items = data.get("items", []) if isinstance(data, dict) else data
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        link = it.get("url") or it.get("link") or it.get("external_url")
        title = it.get("title") or it.get("name")
        if not link or not title:
            continue
        out.append({"url": link, "title": clean_html(str(title)), "author": (it.get("author") or {}).get("name")
                    if isinstance(it.get("author"), dict) else it.get("author"),
                    "published": to_iso(it.get("date_published") or it.get("date") or it.get("published")),
                    "content": clean_html(it.get("content_html") or it.get("content_text") or it.get("description")
                                          or it.get("summary") or ""),
                    "image": it.get("image")})
    return out


def parse_remoteok(raw: bytes) -> list[dict]:
    data = json.loads(raw)
    out = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict) or not it.get("position"):
            continue      # first element is a legal notice
        salary = ""
        if it.get("salary_min") and it.get("salary_max"):
            salary = f"Salary: ${it['salary_min']:,}–${it['salary_max']:,} per year. "
        tags = ", ".join(it.get("tags") or [])
        body = (f"{it.get('company', '')} is hiring a {it['position']} (remote, {it.get('location') or 'worldwide'}). "
                f"{salary}Skills: {tags}. " + clean_html(it.get("description") or ""))
        out.append({"url": it.get("url") or it.get("apply_url"), "title": f"{it['position']} at {it.get('company', '')}",
                    "author": it.get("company"), "published": to_iso(it.get("date") or it.get("epoch")),
                    "content": body, "image": it.get("company_logo") or None})
    return [o for o in out if o["url"]]


PARSERS = {"rss": parse_rss, "atom": parse_rss, "json": parse_json_feed, "remoteok": parse_remoteok}


# ---------------------------------------------------------------------------
# Storing
# ---------------------------------------------------------------------------
def store_entries(conn, source: dict, entries: list[dict], *, ignore_older_than_hours: int = 72,
                  first_fetch: bool = False, now: datetime | None = None) -> int:
    """Insert new entries; returns how many were new. Old/backlog entries are stored as archived."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=ignore_older_than_hours)
    backlog_cutoff = now - timedelta(hours=24)
    new = 0
    for e in entries:
        canon = canonical_url(e["url"])
        th = title_hash(e["title"])
        if conn.execute("SELECT 1 FROM items WHERE canonical_url=?", (canon,)).fetchone():
            continue
        # Same headline from the same channel in the last 3 days = syndicated copy
        dup = conn.execute(
            "SELECT 1 FROM items WHERE channel=? AND title_hash=? AND discovered_at > ?",
            (source["channel"], th, (now - timedelta(days=3)).isoformat())).fetchone()
        if dup:
            continue
        published = e.get("published")
        pub_dt = parse_iso(published)
        if pub_dt and pub_dt > now + timedelta(hours=6):      # bogus future dates
            published, pub_dt = now.isoformat(), now
        archived = 0
        if pub_dt and (pub_dt < cutoff or (first_fetch and pub_dt < backlog_cutoff)):
            archived = 1
        content = (e.get("content") or "")[:20000]
        cur = conn.execute(
            """INSERT INTO items(channel, source_id, url, canonical_url, title_hash, original_title, author, image_url,
                 published_at, discovered_at, content, lang, archived)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source["channel"], source["id"], e["url"], canon, th, squash(e["title"])[:400], e.get("author"),
             e.get("image"), published or now.replace(microsecond=0).isoformat(), now.replace(microsecond=0).isoformat(),
             content, source.get("lang") or detect_script_lang(e["title"] + " " + content), archived))
        index_item(conn, cur.lastrowid)
        new += 1
    return new


def fetch_source(source: dict, fetcher: Fetcher) -> tuple[dict, list[dict] | None, str | None]:
    try:
        raw = fetcher(source["url"])
        parser = PARSERS.get(source["kind"], parse_rss)
        return source, parser(raw), None
    except Exception as exc:  # noqa: BLE001 — one broken source must never stop the others
        msg = f"{exc.__class__.__name__}: {str(exc)[:200]}"
        return source, None, msg


def collect(settings, channel: str | None = None, fetcher: Fetcher = http_fetch, workers: int = 8,
            url_prefix: str | None = None) -> dict:
    """Fetch all enabled sources in parallel and store new items. Returns stats."""
    with session(settings.db_path) as conn:
        q = "SELECT * FROM sources WHERE enabled=1" + (" AND channel=?" if channel else "")
        sources = [dict(r) for r in conn.execute(q, (channel,) if channel else ())]
    if url_prefix:
        sources = [s for s in sources if s["url"].startswith(url_prefix)]
    stats = {"sources": len(sources), "ok": 0, "failed": 0, "new_items": 0, "errors": {}}
    if not sources:
        return stats
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda s: fetch_source(s, fetcher), sources))
    hours = int(settings.pipeline("ignore_older_than_hours", 72))
    with session(settings.db_path) as conn:
        for source, entries, error in results:
            now = utcnow()
            if error is not None:
                stats["failed"] += 1
                stats["errors"][source["name"]] = error
                conn.execute("UPDATE sources SET last_fetched_at=?, last_error=?, fail_count=fail_count+1 WHERE id=?",
                             (now, error, source["id"]))
                continue
            first = source["last_ok_at"] is None
            n = store_entries(conn, source, entries, ignore_older_than_hours=hours, first_fetch=first)
            stats["ok"] += 1
            stats["new_items"] += n
            conn.execute("""UPDATE sources SET last_fetched_at=?, last_ok_at=?, last_error=NULL, fail_count=0,
                            items_total=items_total+? WHERE id=?""", (now, now, n, source["id"]))
    log.info("collect: %s", {k: v for k, v in stats.items() if k != "errors"})
    return stats


# ---------------------------------------------------------------------------
# Full text: only when the feed gave us a teaser
# ---------------------------------------------------------------------------
SKIP_FULLTEXT_HOSTS = re.compile(r"(news\.google\.com|twitter\.com|x\.com|youtube\.com|reddit\.com|remoteok\.com)")


def extract_main_text(html: str) -> str:
    try:
        import trafilatura  # optional dependency
        text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_recall=True)
        if text:
            return text
    except ImportError:
        pass
    paras = re.findall(r"(?is)<p[^>]*>(.*?)</p>", html)
    return "\n\n".join(p for p in (clean_html(x) for x in paras) if len(p) > 60)


def fetch_full_text(url: str, fetcher: Fetcher = http_fetch) -> str:
    if SKIP_FULLTEXT_HOSTS.search(url):
        return ""
    try:
        html = fetcher(url).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        log.debug("full text failed for %s: %s", url, exc)
        return ""
    return extract_main_text(html)[:20000]
