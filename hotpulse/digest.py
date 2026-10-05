"""Daily and weekly briefings (rule-based ranking; the model only writes a short intro) + social post drafts."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Settings, render
from .db import jdumps, jloads, row_to_dict, session, utcnow
from .text import no_em_dash, sentences, truncate

log = logging.getLogger("hotpulse.digest")

DAILY_MAIN, DAILY_BRIEFS, WEEKLY_MAIN = 12, 10, 20
X_TEXT_LIMIT = 252          # X counts any link as 23 chars + 2 newlines -> stays under 280
MAX_PER_SOURCE = 2


def _tz(settings: Settings) -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def period_for(settings: Settings, kind: str, now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """Most recent completed period: (start_utc, end_utc, slug)."""
    tz = _tz(settings)
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    hour = int(settings.schedule("daily_digest_hour", 8))
    end = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if end > now_local:
        end -= timedelta(days=1)
    if kind == "weekly":
        weekday = int(settings.schedule("weekly_digest_weekday", 0))
        end -= timedelta(days=(end.weekday() - weekday) % 7)
        start = end - timedelta(days=7)
        iso = (end - timedelta(days=1)).isocalendar()
        slug = f"{iso[0]}-W{iso[1]:02d}"
    else:
        start = end - timedelta(days=1)
        slug = end.date().isoformat()
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), slug


def _entry(row: dict, also: list[dict]) -> dict:
    facts = jloads(row.get("facts"), {}) or {}
    return {"item_id": row["id"], "event_id": row["event_id"], "title": row["title"] or row["original_title"],
            "summary": row["summary"], "why": row["why_it_matters"], "url": row["url"], "source": row["source"],
            "category": row["category"], "score": row["score"], "source_count": row.get("source_count") or 1,
            "deadline": row.get("deadline"), "funding": facts.get("funding"), "open_to_pakistan": facts.get("open_to_pakistan"),
            "also": [{"item_id": a["id"], "title": a["title"] or a["original_title"], "url": a["url"],
                      "source": a["source"]} for a in also[:4]]}


def rank_entries(rows: list[dict]) -> list[tuple[dict, list[dict]]]:
    """One entry per event: best report leads, other reports listed under it."""
    by_event: dict[int, list[dict]] = {}
    for r in rows:
        by_event.setdefault(r["event_id"] or -r["id"], []).append(r)
    tier_rank = {"T1": 0, "T2": 1, "T3": 2}
    groups = []
    for reports in by_event.values():
        reports.sort(key=lambda r: (tier_rank.get(r["tier"], 1), -(r["score"] or 0)))
        lead = reports[0]
        importance = max(r["score"] or 0 for r in reports) + 6 * (lead.get("source_count") or 1)
        groups.append((importance, lead, reports[1:]))
    groups.sort(key=lambda g: -g[0])
    return [(lead, also) for _, lead, also in groups]


_NUM = re.compile(r"\d[\d,.]*")


def intro_is_grounded(intro: str, source_text: str) -> bool:
    """Reject intros that mention numbers not present in the items (cheap anti-hallucination check)."""
    return all(n.strip(".,") in source_text for n in _NUM.findall(intro))


def template_intro(entries: list[dict], kind: str) -> str:
    if not entries:
        return ""
    lead = entries[0]["title"]
    rest = [e["title"] for e in entries[1:3]]
    when = "Today" if kind == "daily" else "This week"
    text = f"{when}'s top story: {lead}."
    if rest:
        text += " Also: " + "; ".join(rest) + "."
    return text


def build_digest(settings: Settings, llm, channel: str, kind: str = "daily", *, now: datetime | None = None,
                 start: datetime | None = None, end: datetime | None = None, slug: str | None = None,
                 force: bool = False) -> dict | None:
    ch = settings.channels[channel]
    if start is None or end is None or slug is None:
        start, end, slug = period_for(settings, kind, now)
    with session(settings.db_path) as conn:
        exists = conn.execute("SELECT id FROM digests WHERE channel=? AND kind=? AND slug=?", (channel, kind, slug)).fetchone()
        if exists and not force:
            return None
        rows = [dict(r) for r in conn.execute(
            """SELECT i.*, s.name AS source, s.tier AS tier, e.source_count AS source_count
               FROM items i LEFT JOIN sources s ON s.id=i.source_id LEFT JOIN events e ON e.id=i.event_id
               WHERE i.channel=? AND i.selected=1 AND i.selected_at >= ? AND i.selected_at < ?""",
            (channel, start.isoformat(), end.isoformat()))]
        closing = []
        if ch.is_opportunities:
            today = end.astimezone(_tz(settings)).date()
            closing = [dict(r) for r in conn.execute(
                """SELECT i.*, s.name AS source, s.tier AS tier, 1 AS source_count FROM items i
                   LEFT JOIN sources s ON s.id=i.source_id
                   WHERE i.channel=? AND i.selected=1 AND i.deadline >= ? AND i.deadline <= ?
                   ORDER BY i.deadline ASC LIMIT 12""",
                (channel, today.isoformat(), (today + timedelta(days=10)).isoformat()))]
    if not rows:
        return None
    ranked = rank_entries(rows)
    limit = WEEKLY_MAIN if kind == "weekly" else DAILY_MAIN
    main, briefs, per_source = [], [], {}
    for lead, also in ranked:
        src = lead["source"] or "?"
        if len(main) < limit and per_source.get(src, 0) < MAX_PER_SOURCE:
            main.append(_entry(lead, also))
            per_source[src] = per_source.get(src, 0) + 1
        elif kind == "daily" and len(briefs) < DAILY_BRIEFS:
            briefs.append(_entry(lead, also))
    # sections by category for the page
    sections: dict[str, list[int]] = {}
    for idx, e in enumerate(main[1:], 1):
        sections.setdefault(e["category"] or "other", []).append(idx)

    source_text = " ".join(f"{e['title']} {e['summary'] or ''}" for e in main)
    intro = None
    if llm is not None:
        lines = "\n".join(f"- {e['title']}: {truncate(e['summary'], 200)}" for e in main[:8])
        system = render(settings.prompt("digest_intro"), site_name=settings.name, channel_name=ch.name,
                        period="daily" if kind == "daily" else "weekly", safety=settings.prompt("_safety"),
                        language_name=settings.language_name(settings.primary_language))
        out = llm.chat_json(system, f"<material>\n{lines}\n</material>", temperature=0.4, seed=17) or {}
        cand = truncate(out.get("intro"), 600)
        if cand and intro_is_grounded(cand, source_text):
            intro = cand
    intro = intro or template_intro(main, kind)

    local_end = end.astimezone(_tz(settings))
    if kind == "daily":
        title = f"{ch.name} Daily · {local_end.strftime('%d %b %Y')}"
    else:
        title = f"{ch.name} Weekly · {slug}"
    body = {"lead": main[0] if main else None, "main": main[1:], "briefs": briefs, "sections": sections,
            "closing_soon": [_entry(r, []) for r in closing],
            "stats": {"selected": len(rows), "events": len(ranked),
                      "sources": len({r["source"] for r in rows})}}
    with session(settings.db_path) as conn:
        conn.execute(
            """INSERT INTO digests(channel, kind, period_start, period_end, slug, title, intro, body, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(channel, kind, slug) DO UPDATE SET period_start=excluded.period_start,
                 period_end=excluded.period_end, title=excluded.title, intro=excluded.intro, body=excluded.body,
                 created_at=excluded.created_at""",
            (channel, kind, start.isoformat(), end.isoformat(), slug, title, intro, jdumps(body), utcnow()))
        row = conn.execute("SELECT * FROM digests WHERE channel=? AND kind=? AND slug=?", (channel, kind, slug)).fetchone()
    digest = row_to_dict(row)
    top_ids = [e["item_id"] for e in main[: int(settings.site.get("social", {}).get("posts_per_digest", 5))]]
    make_social_posts(settings, llm, top_ids)
    return digest


def ensure_digests(settings: Settings, llm, now: datetime | None = None) -> list[str]:
    """Build any due daily/weekly digest that doesn't exist yet. Safe to call often."""
    made = []
    for key in settings.channels:
        for kind in ("daily", "weekly"):
            d = build_digest(settings, llm, key, kind, now=now)
            if d:
                made.append(f"{key}:{kind}:{d['slug']}")
    return made


def build_today_preview(settings: Settings, llm, channel: str) -> dict | None:
    """'So far today' digest covering the last 24h, rebuilt on demand (admin button / CLI)."""
    now = datetime.now(timezone.utc)
    slug = now.astimezone(_tz(settings)).date().isoformat() + "-live"
    return build_digest(settings, llm, channel, "daily", start=now - timedelta(hours=24), end=now, slug=slug, force=True)


# ---------------------------------------------------------------------------
# Social post drafts (X + Threads), to grow your accounts with your own site's content
# ---------------------------------------------------------------------------
def template_posts(item: dict, hashtags: list[str]) -> dict:
    title = item["title"] or item["original_title"]
    first = (sentences(item.get("summary") or "") or [item.get("summary") or ""])[0]
    tags = " ".join(hashtags[:2])
    x = f"{title}\n\n{truncate(first, 150)}\n\n{tags}".strip()
    if len(x) > X_TEXT_LIMIT:
        x = f"{truncate(title, X_TEXT_LIMIT - 30)}\n\n{tags}".strip()
    threads = f"{title}\n\n{truncate(item.get('summary') or '', 330)}\n\nWhat do you think?".strip()
    return {"x": x[:X_TEXT_LIMIT], "threads": threads[:450]}


def make_social_posts(settings: Settings, llm, item_ids: list[int], lang: str | None = None) -> int:
    lang = lang or settings.primary_language
    made = 0
    for item_id in item_ids:
        with session(settings.db_path) as conn:
            if conn.execute("SELECT 1 FROM social_posts WHERE item_id=? AND lang=? LIMIT 1", (item_id, lang)).fetchone():
                continue
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            continue
        item = dict(row)
        ch = settings.channels.get(item["channel"])
        if not ch:
            continue
        hashtags = ch.hashtags or settings.site.get("social", {}).get("hashtags", [])
        posts = None
        if llm is not None:
            system = render(settings.prompt("social"), site_name=settings.name, channel_name=ch.name,
                            hashtags=" ".join(hashtags), safety=settings.prompt("_safety"),
                            language_name=settings.language_name(lang))
            user = f"<material>\nHeadline: {item['title']}\nSummary: {item['summary']}\nWhy it matters: {item['why_it_matters'] or ''}\n</material>"
            out = llm.chat_json(system, user, temperature=0.7, seed=21) or {}
            if out.get("x") and out.get("threads"):
                posts = {"x": truncate(str(out["x"]), X_TEXT_LIMIT), "threads": no_em_dash(str(out["threads"]).strip())[:480]}
        posts = posts or template_posts(item, hashtags)
        with session(settings.db_path) as conn:
            for platform, text in posts.items():
                conn.execute("INSERT OR IGNORE INTO social_posts(item_id, platform, lang, text, created_at) VALUES (?,?,?,?,?)",
                             (item_id, platform, lang, text, utcnow()))
        made += 1
    return made
