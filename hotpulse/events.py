"""Group reports about the same real-world event, then rank events by heat.

Heat is driven by *how many different sources* cover an event (weighted by source tier),
not by how many articles one site publishes. This is the core idea of AIHOT, kept here.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone

from .config import Settings, render
from .db import jloads, session, utcnow
from .text import jaccard, overlap, parse_iso, token_set, tokens

log = logging.getLogger("hotpulse.events")

TIER_WEIGHT = {"T1": 1.6, "T2": 1.0, "T3": 0.6}
JOIN_AT = 0.55          # similarity that joins without asking the model
ASK_FROM = 0.30         # grey zone: ask the model (if available)
HALF_LIFE_HOURS = 24.0


_PROPER = re.compile(r"\b(?:[A-Z][\w\-\.]*[A-Za-z0-9]|\w*\d\w*)\b")


def _proper_tokens(title: str) -> set[str]:
    """Names/model ids in a headline (capitalised words or words with digits), lowercased."""
    out = set()
    for w in _PROPER.findall(title or ""):
        for t in tokens(w):
            out.add(t)
    return out


def _sig(row: dict) -> dict:
    ents = jloads(row.get("entities"), []) if isinstance(row.get("entities"), str) else (row.get("entities") or [])
    title = f"{row.get('title') or ''} {row.get('original_title') or ''}"
    return {"tokens": token_set(title), "body": token_set((row.get("summary") or "")[:500]),
            "proper": _proper_tokens(title), "ents": {e.lower() for e in ents}}


def similarity(a: dict, b: dict, df: dict[str, int] | None = None) -> float:
    """0..1 lexical similarity. Shared *rare* names (e.g. a startup or model name) are a strong signal."""
    s = max(jaccard(a["tokens"], b["tokens"]), 0.7 * overlap(a["tokens"], b["tokens"]),
            0.6 * overlap(a["body"], b["body"]))
    if a["ents"] and b["ents"]:
        s += 0.25 * jaccard(a["ents"], b["ents"])
    if df is not None:
        rare = {t for t in a["proper"] & b["proper"] if df.get(t, 0) <= 3 and len(t) > 2}
        if rare:
            s += 0.35 if len(rare) == 1 else 0.5
    return min(1.0, s)


def cosine(u: list[float], v: list[float]) -> float:
    dot = sum(x * y for x, y in zip(u, v))
    nu, nv = math.sqrt(sum(x * x for x in u)), math.sqrt(sum(y * y for y in v))
    return dot / (nu * nv) if nu and nv else 0.0


def ask_same_event(settings: Settings, llm, a: dict, b: dict) -> bool:
    system = render(settings.prompt("same_event"), safety=settings.prompt("_safety"))
    user = ("<material>\nReport A: " + f"{a.get('title') or a.get('original_title')}\n{(a.get('summary') or '')[:400]}"
            "\nReport B: " + f"{b.get('title') or b.get('original_title')}\n{(b.get('summary') or '')[:400]}\n</material>")
    out = llm.chat_json(system, user, temperature=0.0, seed=13) or {}
    try:
        conf = float(out.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    return bool(out.get("same")) and conf >= 0.6


def group_pending(settings: Settings, llm=None) -> dict:
    window = int(settings.pipeline("event_window_days", 7))
    since = (datetime.now(timezone.utc) - timedelta(days=window)).isoformat()
    stats = {"grouped": 0, "new_events": 0, "joined": 0, "asked_model": 0}
    with session(settings.db_path) as conn:
        pending = [dict(r) for r in conn.execute(
            """SELECT id, channel, title, original_title, summary, entities, published_at, discovered_at
               FROM items WHERE status='analyzed' AND event_id IS NULL AND archived=0
               ORDER BY COALESCE(published_at, discovered_at) ASC""")]
        if not pending:
            return stats
        # candidate pool per channel: recent items that already belong to an event
        pool: dict[str, list[dict]] = {}
        for r in conn.execute(
                """SELECT i.id, i.channel, i.title, i.original_title, i.summary, i.entities, i.event_id
                   FROM items i JOIN events e ON e.id=i.event_id WHERE e.last_seen > ?""", (since,)):
            d = dict(r)
            d["sig"] = _sig(d)
            pool.setdefault(d["channel"], []).append(d)

        # document frequency of headline names across everything we're comparing
        df: dict[str, int] = {}
        for r in pending + [p for rows in pool.values() for p in rows]:
            for t in _sig(r)["proper"]:
                df[t] = df.get(t, 0) + 1

        embed_cache: dict[int, list[float]] = {}
        if llm is not None and getattr(llm, "embed_model", ""):
            all_rows = pending + [p for rows in pool.values() for p in rows]
            vecs = llm.embed([f"{r.get('title') or r['original_title']}. {(r.get('summary') or '')[:300]}" for r in all_rows])
            if vecs:
                embed_cache = {r["id"]: v for r, v in zip(all_rows, vecs)}

        touched: set[int] = set()
        for item in pending:
            sig = _sig(item)
            best, best_sim = None, 0.0
            for cand in pool.get(item["channel"], []):
                sim = similarity(sig, cand["sig"], df)
                if item["id"] in embed_cache and cand["id"] in embed_cache:
                    sim = max(sim, cosine(embed_cache[item["id"]], embed_cache[cand["id"]]) - 0.25)
                if sim > best_sim:
                    best, best_sim = cand, sim
            event_id = None
            if best is not None and best_sim >= JOIN_AT:
                event_id = best["event_id"]
            elif best is not None and best_sim >= ASK_FROM and llm is not None:
                stats["asked_model"] += 1
                if ask_same_event(settings, llm, item, best):
                    event_id = best["event_id"]
            when = item.get("published_at") or item["discovered_at"]
            if event_id is None:
                cur = conn.execute("INSERT INTO events(channel, lead_item_id, title, first_seen, last_seen, updated_at) "
                                   "VALUES (?,?,?,?,?,?)", (item["channel"], item["id"], item.get("title") or
                                                             item["original_title"], when, when, utcnow()))
                event_id = cur.lastrowid
                stats["new_events"] += 1
            else:
                stats["joined"] += 1
            conn.execute("UPDATE items SET event_id=? WHERE id=?", (event_id, item["id"]))
            item.update(event_id=event_id, sig=sig)
            pool.setdefault(item["channel"], []).append(item)
            touched.add(event_id)
            stats["grouped"] += 1
        for eid in touched:
            recompute_event(conn, eid)
    return stats


def compute_heat(sources: list[tuple[str, str]], item_count: int, max_score: int, last_seen: datetime,
                 first_seen: datetime, now: datetime | None = None) -> float:
    """sources: distinct (source_name, tier). Returns a 0-100ish heat value."""
    now = now or datetime.now(timezone.utc)
    breadth = sum(TIER_WEIGHT.get(t, 0.8) for _, t in sources)
    breadth += 0.25 * max(0, item_count - len(sources))          # extra articles from same sources count a little
    quality = 0.5 + max_score / 100.0
    age_h = max(0.0, (now - last_seen).total_seconds() / 3600)
    decay = 0.5 ** (age_h / HALF_LIFE_HOURS)
    if (now - first_seen) > timedelta(days=3):
        decay *= 0.7                                              # long-running stories cool down
    return round(breadth * quality * decay * 10, 2)


def recompute_event(conn, event_id: int, now: datetime | None = None) -> None:
    rows = [dict(r) for r in conn.execute(
        """SELECT i.id, i.title, i.original_title, i.score, i.selected, i.published_at, i.discovered_at,
                  s.name AS source, s.tier AS tier
           FROM items i LEFT JOIN sources s ON s.id=i.source_id WHERE i.event_id=?""", (event_id,))]
    if not rows:
        conn.execute("DELETE FROM events WHERE id=?", (event_id,))
        return
    times = [parse_iso(r["published_at"] or r["discovered_at"]) for r in rows]
    times = [t for t in times if t] or [datetime.now(timezone.utc)]
    sources = {(r["source"] or "unknown", r["tier"] or "T2") for r in rows}
    tier_rank = {"T1": 0, "T2": 1, "T3": 2}
    # Lead report: selected first, then official sources, then higher score, then earliest
    lead = sorted(rows, key=lambda r: (-r["selected"], tier_rank.get(r["tier"], 1), -(r["score"] or 0),
                                       r["published_at"] or r["discovered_at"]))[0]
    max_score = max((r["score"] or 0) for r in rows)
    heat = compute_heat(sorted(sources), len(rows), max_score, max(times), min(times), now)
    conn.execute("""UPDATE events SET lead_item_id=?, title=?, first_seen=?, last_seen=?, item_count=?, source_count=?,
                    max_score=?, heat=?, updated_at=? WHERE id=?""",
                 (lead["id"], lead["title"] or lead["original_title"], min(times).isoformat(), max(times).isoformat(),
                  len(rows), len(sources), max_score, heat, utcnow(), event_id))


def refresh_heat(settings: Settings, days: int = 10) -> int:
    """Heat decays with time, so recompute recent events every cycle."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with session(settings.db_path) as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM events WHERE last_seen > ?", (since,))]
        for eid in ids:
            recompute_event(conn, eid)
    return len(ids)
