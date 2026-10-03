"""Read-only queries shared by the web pages, JSON API, RSS and MCP. Pages never call the model."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from .db import jloads, row_to_dict

ITEM_COLS = """i.id, i.channel, i.url, i.original_title, i.title, i.summary, i.why_it_matters, i.category, i.tags,
  i.entities, i.score, i.selected, i.selected_at, i.published_at, i.discovered_at, i.image_url, i.author, i.event_id,
  i.deadline, i.facts, i.story_type, i.analyzed_by, s.name AS source, s.tier AS tier, e.source_count AS event_sources,
  e.item_count AS event_items, e.heat AS heat"""
ITEM_FROM = "items i LEFT JOIN sources s ON s.id=i.source_id LEFT JOIN events e ON e.id=i.event_id"


def _apply_translations(conn, items: list[dict], lang: str | None) -> list[dict]:
    if not lang or not items:
        return items
    ids = [it["id"] for it in items]
    marks = ",".join("?" * len(ids))
    tr = {r["item_id"]: r for r in conn.execute(
        f"SELECT * FROM translations WHERE lang=? AND item_id IN ({marks})", (lang, *ids))}
    for it in items:
        t = tr.get(it["id"])
        it["translated"] = bool(t)
        if t:
            it["title_tr"], it["summary_tr"], it["why_tr"] = t["title"], t["summary"], t["why_it_matters"]
    return items


def _items(conn, where: str, params: tuple, order: str, limit: int, offset: int = 0,
           lang: str | None = None) -> list[dict]:
    rows = conn.execute(f"SELECT {ITEM_COLS} FROM {ITEM_FROM} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                        (*params, limit, offset)).fetchall()
    return _apply_translations(conn, [row_to_dict(r) for r in rows], lang)


def latest(conn, channel: str | None = None, *, selected_only: bool = True, category: str | None = None,
           limit: int = 30, offset: int = 0, lang: str | None = None, days: int | None = None) -> list[dict]:
    where = ["i.status='analyzed'", "i.archived=0"]
    params: list = []
    if selected_only:
        where.append("i.selected=1")
    if channel:
        where.append("i.channel=?")
        params.append(channel)
    if category:
        where.append("i.category=?")
        params.append(category)
    if days:
        where.append("i.discovered_at > ?")
        params.append((datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
    order = "COALESCE(i.selected_at, i.discovered_at) DESC" if selected_only else "i.discovered_at DESC"
    return _items(conn, " AND ".join(where), tuple(params), order, limit, offset, lang)


def collapse_by_event(items: list[dict]) -> list[dict]:
    """Show one card per event in lists; the others become 'also covered by'."""
    out, seen = [], {}
    for it in items:
        eid = it.get("event_id")
        if eid and eid in seen:
            seen[eid].setdefault("also", []).append(it)
            continue
        if eid:
            seen[eid] = it
        out.append(it)
    return out


def item(conn, item_id: int, lang: str | None = None) -> dict | None:
    rows = _items(conn, "i.id=?", (item_id,), "i.id", 1, 0, lang)
    return rows[0] if rows else None


def event_items(conn, event_id: int, lang: str | None = None) -> list[dict]:
    return _items(conn, "i.event_id=? AND i.status='analyzed'", (event_id,),
                  "i.selected DESC, CASE s.tier WHEN 'T1' THEN 0 WHEN 'T2' THEN 1 ELSE 2 END, i.score DESC", 50, 0, lang)


def event(conn, event_id: int) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())


def hot_events(conn, channel: str | None = None, *, limit: int = 20, days: int = 3, lang: str | None = None) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = """SELECT e.* FROM events e WHERE e.last_seen > ? AND EXISTS
             (SELECT 1 FROM items i WHERE i.event_id=e.id AND i.selected=1)"""
    params: list = [since]
    if channel:
        q += " AND e.channel=?"
        params.append(channel)
    q += " ORDER BY e.heat DESC LIMIT ?"
    params.append(limit)
    events = [row_to_dict(r) for r in conn.execute(q, params)]
    leads = {}
    ids = [e["lead_item_id"] for e in events if e["lead_item_id"]]
    if ids:
        for it in _items(conn, f"i.id IN ({','.join('?' * len(ids))})", tuple(ids), "i.id", len(ids), 0, lang):
            leads[it["id"]] = it
    for e in events:
        e["lead"] = leads.get(e["lead_item_id"])
        e["sources"] = [r["name"] for r in conn.execute(
            "SELECT DISTINCT s.name FROM items i JOIN sources s ON s.id=i.source_id WHERE i.event_id=? LIMIT 8", (e["id"],))]
    return [e for e in events if e["lead"]]


def open_opportunities(conn, *, within_days: int | None = None, limit: int = 30, lang: str | None = None,
                       open_to_pakistan: bool = False, category: str | None = None) -> list[dict]:
    today = date.today().isoformat()
    where = ["i.selected=1", "i.status='analyzed'", "i.deadline IS NOT NULL", "i.deadline >= ?"]
    params: list = [today]
    if within_days is not None:
        where.append("i.deadline <= ?")
        params.append((date.today() + timedelta(days=within_days)).isoformat())
    if category:
        where.append("i.category=?")
        params.append(category)
    items = _items(conn, " AND ".join(where), tuple(params), "i.deadline ASC", limit * 2, 0, lang)
    if open_to_pakistan:
        items = [it for it in items if (it.get("facts") or {}).get("open_to_pakistan") is not False]
    return collapse_by_event(items)[:limit]


_FTS_SAFE = re.compile(r"[^\w\s\-]", re.UNICODE)


def search(conn, q: str, channel: str | None = None, limit: int = 40, lang: str | None = None) -> list[dict]:
    terms = [t for t in _FTS_SAFE.sub(" ", q).split() if t]
    if not terms:
        return []
    fts = " ".join(f'"{t}"*' for t in terms[:8])
    where = "i.id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?) AND i.status='analyzed'"
    params: list = [fts]
    if channel:
        where += " AND i.channel=?"
        params.append(channel)
    return _items(conn, where, tuple(params), "i.selected DESC, i.discovered_at DESC", limit, 0, lang)


def digests(conn, channel: str | None = None, kind: str | None = None, limit: int = 30) -> list[dict]:
    q, params = "SELECT id, channel, kind, slug, title, intro, period_start, period_end, created_at FROM digests WHERE 1=1", []
    if channel:
        q += " AND channel=?"
        params.append(channel)
    if kind:
        q += " AND kind=?"
        params.append(kind)
    q += " ORDER BY period_end DESC, channel LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(q, params)]


def digest(conn, channel: str, kind: str, slug: str) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM digests WHERE channel=? AND kind=? AND slug=?",
                                    (channel, kind, slug)).fetchone())


def latest_digest(conn, channel: str, kind: str = "daily") -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM digests WHERE channel=? AND kind=? ORDER BY period_end DESC LIMIT 1",
                                    (channel, kind)).fetchone())


def channel_counts(conn) -> dict[str, dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    out = {}
    for r in conn.execute("""SELECT channel, SUM(selected=1 AND selected_at > ?) AS today, SUM(selected=1) AS total
                             FROM items GROUP BY channel""", (since,)):
        out[r["channel"]] = {"today": r["today"] or 0, "total": r["total"] or 0}
    return out


def category_counts(conn, channel: str) -> dict[str, int]:
    return {r["category"]: r["n"] for r in conn.execute(
        "SELECT category, COUNT(*) n FROM items WHERE channel=? AND selected=1 AND category IS NOT NULL GROUP BY category",
        (channel,))}


def stats(conn) -> dict:
    one = lambda q, *p: conn.execute(q, p).fetchone()[0]  # noqa: E731
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return {
        "items": one("SELECT COUNT(*) FROM items"),
        "items_24h": one("SELECT COUNT(*) FROM items WHERE discovered_at > ?", since),
        "selected": one("SELECT COUNT(*) FROM items WHERE selected=1"),
        "selected_24h": one("SELECT COUNT(*) FROM items WHERE selected=1 AND selected_at > ?", since),
        "pending": one("SELECT COUNT(*) FROM items WHERE status='new' AND archived=0"),
        "failed": one("SELECT COUNT(*) FROM items WHERE status='failed'"),
        "blocked": one("SELECT COUNT(*) FROM items WHERE status='blocked'"),
        "events": one("SELECT COUNT(*) FROM events"),
        "sources": one("SELECT COUNT(*) FROM sources WHERE enabled=1"),
        "sources_failing": one("SELECT COUNT(*) FROM sources WHERE enabled=1 AND fail_count > 0"),
        "translations": one("SELECT COUNT(*) FROM translations"),
        "digests": one("SELECT COUNT(*) FROM digests"),
        "cache": one("SELECT COUNT(*) FROM llm_cache"),
    }


def public_item(it: dict, base_url: str) -> dict:
    """Stable JSON shape for API / MCP consumers."""
    facts = it.get("facts") or {}
    out = {
        "id": it["id"], "channel": it["channel"], "title": it.get("title") or it.get("original_title"),
        "original_title": it.get("original_title"), "summary": it.get("summary"),
        "why_it_matters": it.get("why_it_matters"), "category": it.get("category"), "tags": it.get("tags") or [],
        "entities": it.get("entities") or [], "score": it.get("score"), "selected": bool(it.get("selected")),
        "source": it.get("source"), "source_tier": it.get("tier"), "url": it.get("url"),
        "published_at": it.get("published_at"), "discovered_at": it.get("discovered_at"),
        "event_id": it.get("event_id"), "event_sources": it.get("event_sources"),
        "page": f"{base_url}/item/{it['id']}",
    }
    if it.get("translated"):
        out["translation"] = {"title": it.get("title_tr"), "summary": it.get("summary_tr"),
                              "why_it_matters": it.get("why_tr")}
    if it.get("deadline") or facts:
        out["opportunity"] = {"deadline": it.get("deadline"), **{k: facts.get(k) for k in
                              ("organization", "location", "eligibility", "funding", "open_to_pakistan", "apply_url")}}
    return out
