"""Admin dashboard: run the pipeline, manage sources, review picks, copy social post drafts."""
from __future__ import annotations

import hmac
import threading

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import queries as Q
from ..db import index_item, jloads, utcnow
from ..digest import build_today_preview, make_social_posts
from ..llm import get_llm, ollama_status
from ..pipeline import run_cycle
from .common import admin_cookie_value, db, is_admin, render, settings_of

router = APIRouter(prefix="/admin", include_in_schema=False)
_bg_lock = threading.Lock()
_bg_state = {"running": False, "last": None}


def _guard(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


def _back(path: str = "/admin", msg: str | None = None):
    return RedirectResponse(path + (f"?msg={msg}" if msg else ""), status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: int = 0):
    s = settings_of(request)
    weak = s.admin_token in ("", "change-me") and not request.app.state.allow_default_admin
    return render(request, "admin/login.html", {"error": error, "weak": weak})


@router.post("/login")
def login(request: Request, token: str = Form(...)):
    s = settings_of(request)
    weak = s.admin_token in ("", "change-me") and not request.app.state.allow_default_admin
    if weak or not hmac.compare_digest(token.strip(), s.admin_token):
        return RedirectResponse("/admin/login?error=1", status_code=303)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie("hp_admin", admin_cookie_value(s.admin_token), httponly=True, samesite="strict",
                    max_age=60 * 60 * 24 * 30)
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("hp_admin")
    return resp


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, msg: str | None = None):
    _guard(request)
    s = settings_of(request)
    with db(request) as conn:
        stats = Q.stats(conn)
        runs = [dict(r) | {"stats": jloads(r["stats"], {})} for r in
                conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 12")]
        failing = [dict(r) for r in conn.execute(
            "SELECT * FROM sources WHERE enabled=1 AND fail_count>0 ORDER BY fail_count DESC LIMIT 20")]
    status = ollama_status(s.ollama_url, s.ollama_model, force=True) if s.llm_mode != "off" else \
        {"ok": False, "detail": "LLM_MODE=off (heuristic mode)"}
    sched = request.app.state.scheduler
    return render(request, "admin/dashboard.html", {
        "stats": stats, "runs": runs, "failing": failing, "llm": status, "msg": msg, "bg": _bg_state,
        "next_run": sched.next_run_at.isoformat() if sched and sched.next_run_at else None})


def _run_bg(settings, **kw) -> None:
    try:
        _bg_state["last"] = run_cycle(settings, **kw)
    finally:
        _bg_state["running"] = False
        _bg_lock.release()


@router.post("/run")
def run_now(request: Request, collect_sources: str = Form("1")):
    _guard(request)
    if not _bg_lock.acquire(blocking=False):
        return _back(msg="A run is already in progress")
    _bg_state["running"] = True
    threading.Thread(target=_run_bg, args=(settings_of(request),),
                     kwargs={"collect_sources": collect_sources == "1"}, daemon=True).start()
    return _back(msg="Pipeline started. Refresh in a minute.")


@router.post("/digest")
def digest_now(request: Request):
    _guard(request)
    s = settings_of(request)
    llm = get_llm(s)
    made = [k for k in s.channels if build_today_preview(s, llm, k)]
    return _back(msg=f"Built live briefings for: {', '.join(made) or 'none (no picks in the last 24h)'}")


# -- sources -----------------------------------------------------------------
@router.get("/sources", response_class=HTMLResponse)
def sources(request: Request, msg: str | None = None):
    _guard(request)
    with db(request) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY channel, enabled DESC, name")]
    return render(request, "admin/sources.html", {"rows": rows, "msg": msg})


@router.post("/sources/{source_id}/toggle")
def toggle_source(request: Request, source_id: int):
    _guard(request)
    with db(request) as conn:
        conn.execute("UPDATE sources SET enabled = 1 - enabled WHERE id=?", (source_id,))
    return _back("/admin/sources")


@router.post("/sources/add")
def add_source(request: Request, channel: str = Form(...), name: str = Form(...), url: str = Form(...),
               tier: str = Form("T2"), kind: str = Form("rss")):
    _guard(request)
    s = settings_of(request)
    if channel not in s.channels or not url.startswith(("http://", "https://")):
        return _back("/admin/sources", "Invalid channel or URL")
    with db(request) as conn:
        conn.execute("""INSERT INTO sources(channel, name, url, kind, tier, enabled, from_config) VALUES (?,?,?,?,?,1,0)
                        ON CONFLICT(channel, url) DO UPDATE SET enabled=1, name=excluded.name""",
                     (channel, name.strip()[:120], url.strip(), kind if kind in ("rss", "json", "remoteok") else "rss",
                      tier if tier in ("T1", "T2", "T3") else "T2"))
    return _back("/admin/sources", "Source added. Tip: also add it to config/channels/*.yaml to keep it in Git.")


@router.post("/sources/{source_id}/delete")
def delete_source(request: Request, source_id: int):
    _guard(request)
    with db(request) as conn:
        row = conn.execute("SELECT from_config FROM sources WHERE id=?", (source_id,)).fetchone()
        if row and not row["from_config"]:
            conn.execute("UPDATE items SET source_id=NULL WHERE source_id=?", (source_id,))
            conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
            return _back("/admin/sources", "Deleted")
    return _back("/admin/sources", "Sources from config files can only be disabled (edit the YAML to remove)")


# -- items -------------------------------------------------------------------
@router.get("/items", response_class=HTMLResponse)
def items(request: Request, channel: str | None = None, status: str | None = None):
    _guard(request)
    q = """SELECT i.id, i.channel, i.original_title, i.title, i.score1, i.score2, i.score, i.score_reason, i.selected,
                  i.status, i.prefilter, i.category, i.discovered_at, i.analyzed_by, i.error, i.archived, s.name AS source,
                  s.tier AS tier FROM items i LEFT JOIN sources s ON s.id=i.source_id WHERE 1=1"""
    params: list = []
    if channel:
        q += " AND i.channel=?"
        params.append(channel)
    if status:
        q += " AND i.status=?"
        params.append(status)
    q += " ORDER BY i.discovered_at DESC LIMIT 200"
    with db(request) as conn:
        rows = [dict(r) for r in conn.execute(q, params)]
    return render(request, "admin/items.html", {"rows": rows, "filter_channel": channel, "status": status})


@router.post("/items/{item_id}/select")
def set_selected(request: Request, item_id: int, value: int = Form(...)):
    """Manual override: editors always have the last word."""
    _guard(request)
    with db(request) as conn:
        conn.execute("UPDATE items SET selected=?, selected_at=CASE WHEN ?=1 THEN COALESCE(selected_at, ?) ELSE NULL END "
                     "WHERE id=?", (1 if value else 0, 1 if value else 0, utcnow(), item_id))
        index_item(conn, item_id)
    return RedirectResponse(request.headers.get("referer") or "/admin/items", status_code=303)


@router.post("/items/{item_id}/reanalyze")
def reanalyze(request: Request, item_id: int):
    _guard(request)
    with db(request) as conn:
        conn.execute("UPDATE items SET status='new', attempts=0, error=NULL, archived=0 WHERE id=?", (item_id,))
    return RedirectResponse(request.headers.get("referer") or "/admin/items", status_code=303)


# -- social ------------------------------------------------------------------
@router.get("/social", response_class=HTMLResponse)
def social(request: Request, msg: str | None = None):
    _guard(request)
    with db(request) as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT p.*, i.title, i.channel FROM social_posts p JOIN items i ON i.id=p.item_id
               ORDER BY p.used ASC, p.created_at DESC LIMIT 120""")]
    posts: dict[int, dict] = {}
    for r in rows:
        posts.setdefault(r["item_id"], {"item_id": r["item_id"], "title": r["title"], "channel": r["channel"],
                                        "used": r["used"], "created_at": r["created_at"]})[r["platform"]] = r
    return render(request, "admin/social.html", {"posts": list(posts.values()), "msg": msg})


@router.post("/social/generate")
def social_generate(request: Request):
    _guard(request)
    s = settings_of(request)
    with db(request) as conn:
        ids = [e["lead_item_id"] for e in Q.hot_events(conn, limit=10, days=2)]
    n = make_social_posts(s, get_llm(s), ids)
    return _back("/admin/social", f"Drafted posts for {n} new stories")


@router.post("/social/{item_id}/used")
def social_used(request: Request, item_id: int):
    _guard(request)
    with db(request) as conn:
        conn.execute("UPDATE social_posts SET used=1 WHERE item_id=?", (item_id,))
    return _back("/admin/social")
