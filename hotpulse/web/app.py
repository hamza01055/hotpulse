"""FastAPI application: public pages. API/feeds/MCP live in api.py, admin in admin.py."""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import queries as Q
from ..config import load_settings
from ..llm import ollama_status
from ..pipeline import Scheduler, prepare
from .common import HERE, db, pick_lang, render, settings_of

log = logging.getLogger("hotpulse.web")
PAGE = 30


def create_app(settings=None, *, start_scheduler: bool = False, allow_default_admin: bool = False) -> FastAPI:
    settings = settings or load_settings()
    prepare(settings)
    app = FastAPI(title=settings.name, docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.settings = settings
    app.state.allow_default_admin = allow_default_admin
    app.state.scheduler = None
    app.state.ai_ready = lambda: settings.llm_mode != "off" and ollama_status(settings.ollama_url, settings.ollama_model)["ok"]
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    if start_scheduler:
        @app.on_event("startup")
        def _start() -> None:
            app.state.scheduler = Scheduler(settings)
            app.state.scheduler.start()

        @app.on_event("shutdown")
        def _stop() -> None:
            if app.state.scheduler:
                app.state.scheduler.stop()

    from .admin import router as admin_router
    from .api import router as api_router
    app.include_router(api_router)
    app.include_router(admin_router)
    register_pages(app)
    return app


def register_pages(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        lang = pick_lang(request)
        s = settings_of(request)
        with db(request) as conn:
            hot = Q.hot_events(conn, limit=7, days=2, lang=lang)
            per_channel = {key: Q.collapse_by_event(Q.latest(conn, key, limit=12, lang=lang))[:6] for key in s.channels}
            closing = Q.open_opportunities(conn, within_days=14, limit=6, lang=lang)
            counts = Q.channel_counts(conn)
            dailies = {key: Q.latest_digest(conn, key) for key in s.channels}
        return render(request, "index.html", {"hot": hot, "per_channel": per_channel, "closing": closing,
                                              "counts": counts, "dailies": dailies})

    @app.get("/c/{channel}", response_class=HTMLResponse)
    def channel_page(request: Request, channel: str, view: str = "picks", cat: str | None = None, page: int = 1,
                     pk: int = 0):
        s = settings_of(request)
        ch = s.channels.get(channel)
        if not ch:
            raise HTTPException(404)
        lang = pick_lang(request)
        page = max(1, page)
        with db(request) as conn:
            items = Q.latest(conn, channel, selected_only=(view != "all"), category=cat, limit=PAGE + 1,
                             offset=(page - 1) * PAGE, lang=lang)
            has_more = len(items) > PAGE
            items = items[:PAGE]
            if view != "all":
                items = Q.collapse_by_event(items)
            if ch.is_opportunities and pk:
                items = [i for i in items if (i.get("facts") or {}).get("open_to_pakistan") is not False]
            hot = Q.hot_events(conn, channel, limit=5, days=3, lang=lang)
            cats = Q.category_counts(conn, channel)
            closing = Q.open_opportunities(conn, within_days=10, limit=8, lang=lang) if ch.is_opportunities else []
            daily = Q.latest_digest(conn, channel)
        return render(request, "channel.html", {"ch": ch, "items": items, "view": view, "cat": cat, "page": page,
                                                "has_more": has_more, "hot": hot, "cats": cats, "closing": closing,
                                                "daily": daily, "pk": pk})

    @app.get("/hot", response_class=HTMLResponse)
    def hot_page(request: Request, channel: str | None = None, days: int = 3):
        lang = pick_lang(request)
        with db(request) as conn:
            events = Q.hot_events(conn, channel, limit=40, days=min(max(days, 1), 14), lang=lang)
        return render(request, "hot.html", {"events": events, "filter_channel": channel, "days": days})

    @app.get("/opportunities", response_class=HTMLResponse)
    def opportunities_page(request: Request, within: int | None = None, pk: int = 0, cat: str | None = None):
        lang = pick_lang(request)
        with db(request) as conn:
            items = Q.open_opportunities(conn, within_days=within, limit=100, lang=lang, open_to_pakistan=bool(pk),
                                         category=cat)
        ch = next((c for c in settings_of(request).channels.values() if c.is_opportunities), None)
        return render(request, "opportunities.html", {"items": items, "within": within, "pk": pk, "cat": cat, "ch": ch})

    @app.get("/item/{item_id}", response_class=HTMLResponse)
    def item_page(request: Request, item_id: int):
        lang = pick_lang(request)
        with db(request) as conn:
            it = Q.item(conn, item_id, lang)
            if not it:
                raise HTTPException(404)
            siblings = [x for x in Q.event_items(conn, it["event_id"], lang) if x["id"] != item_id] if it["event_id"] else []
            ev = Q.event(conn, it["event_id"]) if it["event_id"] else None
            related = Q.latest(conn, it["channel"], category=it["category"], limit=6, lang=lang)
            related = [r for r in related if r["id"] != item_id and r.get("event_id") != it.get("event_id")][:4]
        ch = settings_of(request).channels.get(it["channel"])
        return render(request, "item.html", {"it": it, "siblings": siblings, "ev": ev, "related": related, "ch": ch})

    @app.get("/event/{event_id}", response_class=HTMLResponse)
    def event_page(request: Request, event_id: int):
        lang = pick_lang(request)
        with db(request) as conn:
            ev = Q.event(conn, event_id)
            if not ev:
                raise HTTPException(404)
            items = Q.event_items(conn, event_id, lang)
        ch = settings_of(request).channels.get(ev["channel"])
        timeline = sorted(items, key=lambda x: x.get("published_at") or x["discovered_at"])
        return render(request, "event.html", {"ev": ev, "items": items, "timeline": timeline, "ch": ch})

    @app.get("/briefings", response_class=HTMLResponse)
    def briefings(request: Request, channel: str | None = None, kind: str | None = None):
        with db(request) as conn:
            rows = Q.digests(conn, channel, kind, limit=60)
        return render(request, "briefings.html", {"rows": rows, "filter_channel": channel, "kind": kind})

    @app.get("/briefings/{channel}/{kind}/{slug}", response_class=HTMLResponse)
    def briefing(request: Request, channel: str, kind: str, slug: str):
        lang = pick_lang(request)
        with db(request) as conn:
            d = Q.digest(conn, channel, kind, slug)
            if not d:
                raise HTTPException(404)
            ids = [e["item_id"] for e in ([d["body"].get("lead")] if d["body"].get("lead") else []) +
                   d["body"].get("main", []) + d["body"].get("briefs", []) + d["body"].get("closing_soon", [])]
            tr = {}
            if lang != settings_of(request).primary_language and ids:
                marks = ",".join("?" * len(ids))
                tr = {r["item_id"]: dict(r) for r in conn.execute(
                    f"SELECT * FROM translations WHERE lang=? AND item_id IN ({marks})", (lang, *ids))}
        ch = settings_of(request).channels.get(channel)
        return render(request, "briefing.html", {"d": d, "ch": ch, "tr": tr})

    @app.get("/briefings/{channel}/latest")
    def briefing_latest(request: Request, channel: str):
        with db(request) as conn:
            d = Q.latest_digest(conn, channel)
        if not d:
            return RedirectResponse(f"/c/{channel}", status_code=302)
        return RedirectResponse(f"/briefings/{channel}/{d['kind']}/{d['slug']}", status_code=302)

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", channel: str | None = None):
        lang = pick_lang(request)
        results = []
        if q.strip():
            with db(request) as conn:
                results = Q.search(conn, q, channel, lang=lang)
        return render(request, "search.html", {"q": q, "results": results, "filter_channel": channel})

    @app.get("/saved", response_class=HTMLResponse)
    def saved_page(request: Request):
        return render(request, "saved.html")

    @app.get("/about", response_class=HTMLResponse)
    def about_page(request: Request):
        return render(request, "about.html")

    @app.get("/developers", response_class=HTMLResponse)
    def developers_page(request: Request):
        return render(request, "developers.html")

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return "ok"

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        if request.url.path.startswith(("/api/", "/mcp", "/feed")):
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "not found"}, status_code=404)
        return render(request, "404.html", status_code=404)
