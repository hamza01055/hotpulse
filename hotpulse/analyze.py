"""Judge and write: prefilter -> two independent scores -> structure + headline/summary -> select.

Every step has an AI path (Ollama) and a transparent heuristic path used when no model is available.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from .config import Channel, Settings, render
from .db import index_item, jdumps, session, utcnow
from .text import (guess_deadline, keyword_hits, normalise_deadline, sentences, squash, token_set, tokens,
                   truncate)
from .collect import fetch_full_text, http_fetch

log = logging.getLogger("hotpulse.analyze")

MAX_MATERIAL_CHARS = 6000
HEURISTIC_REASON = "keyword heuristic (no AI model available)"
FUNDING_VALUES = {"fully funded", "partially funded", "paid", "unpaid", "prize", "unknown"}


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------
def material_block(item: dict, source: dict | None) -> str:
    src = source or {}
    body = truncate(item.get("content") or "", MAX_MATERIAL_CHARS)
    return (
        "<material>\n"
        f"Source: {src.get('name', 'unknown')} (tier {src.get('tier', 'T2')})\n"
        f"Published: {item.get('published_at') or 'unknown'}\n"
        f"URL: {item.get('url')}\n"
        f"Title: {item.get('original_title')}\n"
        f"Text: {body or '(no text, title only)'}\n"
        "</material>"
    )


def _base_vars(settings: Settings, ch: Channel, lang: str | None = None) -> dict[str, Any]:
    lang = lang or settings.primary_language
    return {"site_name": settings.name, "channel_name": ch.name, "channel_description": ch.description,
            "rubric": ch.rubric.strip(), "safety": settings.prompt("_safety"), "language_name": settings.language_name(lang),
            "category_keys": ", ".join(c.key for c in ch.categories) or "general",
            "hashtags": " ".join(ch.hashtags), "today": date.today().isoformat()}


# ---------------------------------------------------------------------------
# Heuristics (no-AI mode)
# ---------------------------------------------------------------------------
CLICKBAIT = re.compile(r"(you won'?t believe|shocking|this one trick|\?\!|!!|top \d+ |\d+ best )", re.I)
NUMBERS = re.compile(r"(\$\s?\d|\d+(\.\d+)?\s?(%|percent|million|billion|crore|lakh|bn|m\b)|\bRs\.?\s?\d)", re.I)


def heuristic_prefilter(ch: Channel, item: dict) -> str:
    text = f"{item['original_title']} {(item.get('content') or '')[:1500]}"
    if ch.block_keywords and keyword_hits(item["original_title"], ch.block_keywords):
        return "BLOCK"
    return "PASS" if keyword_hits(text, ch.keywords) else "UNKNOWN"


def heuristic_score(ch: Channel, item: dict, tier: str) -> int:
    title = item["original_title"]
    body = (item.get("content") or "")[:3000]
    score = 30
    score += min(keyword_hits(title, ch.keywords), 3) * 7
    score += min(keyword_hits(body, ch.keywords), 5) * 3
    score += {"T1": 12, "T2": 6}.get(tier, 0)
    if len(body) > 800:
        score += 6
    if len(body) > 2000:
        score += 4
    if NUMBERS.search(title + " " + body):
        score += 5
    if ch.is_opportunities:
        text = (title + " " + body).lower()
        if guess_deadline(title + " " + body):
            score += 8
        if "fully funded" in text or "fully-funded" in text:
            score += 10
        if "pakistan" in text or "international students" in text or "all countries" in text:
            score += 5
    if CLICKBAIT.search(title):
        score -= 10
    if not body:
        score -= 8
    return max(0, min(95, score))


def heuristic_category(ch: Channel, item: dict) -> str | None:
    if not ch.categories:
        return None
    title, body = item["original_title"], (item.get("content") or "")[:2000]
    best = max(ch.categories, key=lambda c: keyword_hits(title, c.keywords) * 3 + keyword_hits(body, c.keywords))
    if keyword_hits(title, best.keywords) * 3 + keyword_hits(body, best.keywords) == 0:
        return ch.categories[-1].key
    return best.key


_ENTITY_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:[ \-][A-Z][a-zA-Z0-9]+)*|[A-Z]{2,}[a-z]?)\b")
_COMMON_CAPS = {"The", "A", "An", "How", "Why", "What", "When", "New", "This", "That", "In", "On", "For", "With",
                "And", "Its", "It", "Is", "Are", "From", "After", "Before", "To", "Of", "By", "At", "Here", "We", "You"}


def heuristic_entities(text: str) -> list[str]:
    seen: list[str] = []
    for m in _ENTITY_RE.findall(text):
        m = m.strip()
        if m in _COMMON_CAPS or len(m) < 2:
            continue
        if m not in seen:
            seen.append(m)
    return seen[:6]


def heuristic_summary(item: dict) -> str:
    title_tokens = token_set(item["original_title"])
    picked = []
    for s in sentences(item.get("content") or ""):
        if len(token_set(s) - title_tokens) < 3:
            continue        # sentence just repeats the headline
        picked.append(s)
        if sum(len(p) for p in picked) > 280 or len(picked) == 3:
            break
    return truncate(" ".join(picked), 480) if picked else truncate(item.get("content") or "", 300)


def heuristic_facts(ch: Channel, item: dict) -> dict:
    text = f"{item['original_title']}\n{item.get('content') or ''}"
    low = text.lower()
    funding = "unknown"
    if re.search(r"fully[\s-]funded", low):
        funding = "fully funded"
    elif re.search(r"partial(ly)?[\s-]funded|tuition (fee )?waiver|partial scholarship", low):
        funding = "partially funded"
    elif re.search(r"\b(prize|cash award|win \$)", low):
        funding = "prize"
    elif re.search(r"\b(salary|per hour|/hr|paid internship|stipend|\$\d)", low):
        funding = "paid"
    elif "unpaid" in low:
        funding = "unpaid"
    open_pk = None
    if "pakistan" in low or re.search(r"all (countries|nationalities)|international (students|applicants)|worldwide|"
                                      r"developing countries|anywhere", low):
        open_pk = True
    location = "Remote" if re.search(r"\bremote\b", low) else ("Online" if "online" in low else None)
    return {"organization": item.get("author"), "deadline": guess_deadline(text), "location": location,
            "eligibility": None, "funding": funding, "open_to_pakistan": open_pk, "apply_url": None}


# ---------------------------------------------------------------------------
# AI steps
# ---------------------------------------------------------------------------
def _clamp_score(value: Any) -> int | None:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def ai_prefilter(llm, settings, ch, mat) -> str | None:
    out = llm.chat_json(render(settings.prompt("prefilter"), **_base_vars(settings, ch)), mat, temperature=0.0, seed=7)
    decision = str((out or {}).get("decision", "")).upper()
    return decision if decision in {"PASS", "BLOCK", "UNKNOWN"} else None


def ai_scores(llm, settings, ch, mat) -> tuple[int | None, int | None, str | None]:
    extra = ""
    if ch.is_opportunities:
        extra = f"Today is {date.today().isoformat()}. Expired deadlines score below 30."
    system = render(settings.prompt("score"), **_base_vars(settings, ch), extra=extra)
    a = llm.chat_json(system, mat, temperature=0.3, seed=11)
    b = llm.chat_json(system, mat, temperature=0.7, seed=29)
    reason = (a or {}).get("reason") or (b or {}).get("reason")
    return _clamp_score((a or {}).get("score")), _clamp_score((b or {}).get("score")), reason


def ai_analyze(llm, settings, ch, mat) -> dict | None:
    v = _base_vars(settings, ch)
    opp_rules, opp_keys = "", ""
    if ch.is_opportunities:
        opp_rules = render(settings.prompt("opportunity_rules"), **v)
        opp_keys = (', "organization": str|null, "deadline": "YYYY-MM-DD"|null, "location": str|null, '
                    '"eligibility": str|null, "funding": str, "open_to_pakistan": bool|null, "apply_url": str|null')
    system = render(settings.prompt("analyze"), **v, opportunity_rules=opp_rules, opportunity_keys=opp_keys)
    out = llm.chat_json(system, mat, temperature=0.2, seed=3)
    if not out or not str(out.get("title", "")).strip() or not str(out.get("summary", "")).strip():
        return None
    return out


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def _as_list(value: Any, limit: int) -> list[str]:
    if isinstance(value, str):
        value = [v for v in re.split(r"[,;]", value)]
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        s = squash(str(v))[:60]
        if s and s.lower() not in {o.lower() for o in out}:
            out.append(s)
    return out[:limit]


def analyze_item(settings: Settings, llm, item: dict, source: dict | None, fetcher=http_fetch) -> dict:
    """Return the column updates for one item (does not write)."""
    ch = settings.channels[item["channel"]]
    tier = (source or {}).get("tier", "T2")

    # 0. Full text when the feed only gave a teaser
    if settings.pipeline("fetch_full_text", True) and len(item.get("content") or "") < 500:
        full = fetch_full_text(item["url"], fetcher)
        if len(full) > len(item.get("content") or ""):
            item["content"] = full

    mat = material_block(item, source)
    upd: dict[str, Any] = {"content": item.get("content"), "analyzed_at": utcnow(),
                           "analyzed_by": llm.name if llm else "heuristic"}

    # 1. Prefilter (title block-list always applies)
    decision = heuristic_prefilter(ch, item)
    if decision != "BLOCK" and llm:
        decision = ai_prefilter(llm, settings, ch, mat) or decision
    upd["prefilter"] = decision
    if decision == "BLOCK":
        upd.update(status="blocked", selected=0)
        return upd

    # 2. Two independent scores
    s1 = s2 = None
    reason = None
    if llm:
        s1, s2, reason = ai_scores(llm, settings, ch, mat)
    if s1 is None and s2 is None:
        s1 = s2 = heuristic_score(ch, item, tier)
        reason = HEURISTIC_REASON
    elif s1 is None or s2 is None:
        s1 = s2 = s1 if s1 is not None else s2
    upd.update(score1=s1, score2=s2, score=(s1 + s2) // 2, score_reason=reason)

    # 3. Structure + headline + summary
    out = ai_analyze(llm, settings, ch, mat) if llm else None
    facts: dict[str, Any] = {}
    if out:
        cat = str(out.get("category", "")).strip().lower()
        upd.update(
            title=truncate(out.get("title"), 200), summary=truncate(out.get("summary"), 900),
            why_it_matters=truncate(out.get("why_it_matters"), 300) or None,
            category=cat if cat in {c.key for c in ch.categories} else heuristic_category(ch, item),
            tags=jdumps([t.lower() for t in _as_list(out.get("tags"), 5)]),
            entities=jdumps(_as_list(out.get("entities"), 6)),
            story_type=out.get("story_type") if out.get("story_type") in {"single", "roundup", "insufficient"} else "single",
        )
        if ch.is_opportunities:
            facts = {k: out.get(k) for k in ("organization", "deadline", "location", "eligibility", "funding",
                                             "open_to_pakistan", "apply_url")}
            facts["deadline"] = normalise_deadline(facts.get("deadline"))
            if str(facts.get("funding", "")).lower() not in FUNDING_VALUES:
                facts["funding"] = "unknown"
            # Only trust links that really appear in the material
            if facts.get("apply_url") and str(facts["apply_url"]) not in mat:
                facts["apply_url"] = None
            if not facts["deadline"]:
                facts["deadline"] = guess_deadline(f"{item['original_title']}\n{item.get('content') or ''}")
    else:
        title_text = f"{item['original_title']} {(item.get('content') or '')[:600]}"
        upd.update(title=item["original_title"], summary=heuristic_summary(item), why_it_matters=None,
                   category=heuristic_category(ch, item),
                   tags=jdumps([k for k in ch.keywords if keyword_hits(title_text, [k])][:4]),
                   entities=jdumps(heuristic_entities(item["original_title"])), story_type="single")
        if ch.is_opportunities:
            facts = heuristic_facts(ch, item)
    if facts:
        upd["facts"] = jdumps(facts)
        upd["deadline"] = facts.get("deadline")

    # 4. Selection: sum of the two scores must reach 2x the tier threshold
    threshold = ch.threshold(tier)
    if not llm or reason == HEURISTIC_REASON:
        # keyword scores are flatter than model scores, so the bar is a little lower
        threshold -= int(settings.pipeline("heuristic_threshold_offset", 8))
    passes = (s1 + s2) >= 2 * threshold
    if upd.get("story_type") == "insufficient":
        passes = False
    if ch.is_opportunities and upd.get("deadline") and upd["deadline"] < date.today().isoformat():
        passes = False      # already closed
    upd["selected"] = 1 if passes else 0
    upd["selected_at"] = utcnow() if passes else None
    upd["status"] = "analyzed"
    return upd


def save_item(conn, item_id: int, upd: dict) -> None:
    cols = ", ".join(f"{k}=?" for k in upd)
    conn.execute(f"UPDATE items SET {cols} WHERE id=?", (*upd.values(), item_id))
    index_item(conn, item_id)


def analyze_pending(settings: Settings, llm, limit: int | None = None, fetcher=http_fetch) -> dict:
    limit = limit or int(settings.pipeline("max_items_per_run", 120))
    with session(settings.db_path) as conn:
        rows = conn.execute(
            """SELECT i.*, s.name AS s_name, s.tier AS s_tier FROM items i LEFT JOIN sources s ON s.id=i.source_id
               WHERE i.status='new' AND i.archived=0 AND i.attempts < 3
               ORDER BY i.discovered_at DESC LIMIT ?""", (limit,)).fetchall()
    stats = {"analyzed": 0, "selected": 0, "blocked": 0, "failed": 0}
    for row in rows:
        item = dict(row)
        if item["channel"] not in settings.channels:
            continue
        source = {"name": item.pop("s_name"), "tier": item.pop("s_tier") or "T2"}
        try:
            upd = analyze_item(settings, llm, item, source, fetcher)
            with session(settings.db_path) as conn:
                save_item(conn, item["id"], upd)
            stats["analyzed"] += 1
            stats["selected"] += upd.get("selected", 0)
            stats["blocked"] += 1 if upd.get("status") == "blocked" else 0
        except Exception as exc:  # noqa: BLE001 (never let one item stop the run)
            log.exception("analyze failed for item %s", item["id"])
            with session(settings.db_path) as conn:
                conn.execute("UPDATE items SET attempts=attempts+1, error=?, status=CASE WHEN attempts>=2 "
                             "THEN 'failed' ELSE status END WHERE id=?", (str(exc)[:300], item["id"]))
            stats["failed"] += 1
    return stats


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------
def translate_item(settings: Settings, llm, item_id: int, lang: str) -> dict | None:
    """Translate one item's headline/summary; cached in the translations table."""
    with session(settings.db_path) as conn:
        hit = conn.execute("SELECT * FROM translations WHERE item_id=? AND lang=?", (item_id, lang)).fetchone()
        if hit:
            return dict(hit)
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not item or not llm or lang == settings.primary_language or not item["title"]:
        return None
    ch = settings.channels.get(item["channel"])
    if not ch:
        return None
    system = render(settings.prompt("translate"), **_base_vars(settings, ch, lang))
    user = (f"<material>\nTitle: {item['title']}\nSummary: {item['summary'] or ''}\n"
            f"Why it matters: {item['why_it_matters'] or ''}\n</material>")
    out = llm.chat_json(system, user, temperature=0.1, seed=5)
    if not out or not out.get("title"):
        return None
    row = {"item_id": item_id, "lang": lang, "title": truncate(out.get("title"), 300),
           "summary": truncate(out.get("summary"), 1200), "why_it_matters": truncate(out.get("why_it_matters"), 400)}
    with session(settings.db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO translations(item_id, lang, title, summary, why_it_matters, created_at) "
                     "VALUES (?,?,?,?,?,?)", (item_id, lang, row["title"], row["summary"], row["why_it_matters"], utcnow()))
    return row


def auto_translate(settings: Settings, llm, limit: int = 40) -> int:
    langs = [l for l in settings.site.get("auto_translate", []) if l != settings.primary_language]
    if not llm or not langs:
        return 0
    done = 0
    for lang in langs:
        with session(settings.db_path) as conn:
            ids = [r["id"] for r in conn.execute(
                """SELECT i.id FROM items i WHERE i.selected=1 AND i.title IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM translations t WHERE t.item_id=i.id AND t.lang=?)
                   ORDER BY i.selected_at DESC LIMIT ?""", (lang, limit))]
        for item_id in ids:
            if translate_item(settings, llm, item_id, lang):
                done += 1
    return done
