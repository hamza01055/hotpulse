"""Public JSON API (/api/v1), RSS feeds, llms.txt / sitemap, and an MCP server for AI agents (/mcp)."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from email.utils import format_datetime
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .. import queries as Q
from ..analyze import translate_item
from ..llm import get_llm
from ..text import parse_iso, truncate
from .common import db, settings_of

router = APIRouter()


def _base(request: Request) -> str:
    return settings_of(request).site_url


def _channel_or_404(request: Request, channel: str | None) -> None:
    if channel and channel not in settings_of(request).channels:
        raise HTTPException(404, f"unknown channel '{channel}'")


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------
@router.get("/api/v1/channels", tags=["api"])
def api_channels(request: Request):
    s = settings_of(request)
    with db(request) as conn:
        counts = Q.channel_counts(conn)
    return {"site": s.name, "languages": list(s.languages), "channels": [
        {"key": c.key, "name": c.name, "description": c.description, "kind": c.kind,
         "categories": [{"key": x.key, "name": x.name} for x in c.categories], **counts.get(c.key, {"today": 0, "total": 0})}
        for c in s.channels.values()]}


@router.get("/api/v1/items", tags=["api"])
def api_items(request: Request, channel: str | None = None, category: str | None = None, all: bool = False,
              limit: int = 30, offset: int = 0, lang: str | None = None):
    _channel_or_404(request, channel)
    with db(request) as conn:
        items = Q.latest(conn, channel, selected_only=not all, category=category, limit=min(max(limit, 1), 100),
                         offset=max(offset, 0), lang=lang)
    return {"items": [Q.public_item(i, _base(request)) for i in items]}


@router.get("/api/v1/items/{item_id}", tags=["api"])
def api_item(request: Request, item_id: int, lang: str | None = None):
    with db(request) as conn:
        it = Q.item(conn, item_id, lang)
    if not it:
        raise HTTPException(404)
    return Q.public_item(it, _base(request))


_rate: dict[str, deque] = defaultdict(deque)
_translate_lock = threading.Lock()


@router.post("/api/v1/items/{item_id}/translate", tags=["api"])
def api_translate(request: Request, item_id: int, lang: str):
    """Translate on demand (cached forever). Rate-limited because local models are slow."""
    s = settings_of(request)
    if lang not in s.languages:
        raise HTTPException(400, "unsupported language")
    ip = request.client.host if request.client else "?"
    window = _rate[ip]
    now = time.time()
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= 10:
        raise HTTPException(429, "too many translations, try again in a minute")
    window.append(now)
    with db(request) as conn:
        hit = conn.execute("SELECT * FROM translations WHERE item_id=? AND lang=?", (item_id, lang)).fetchone()
    if hit:
        return dict(hit)
    llm = get_llm(s)
    if llm is None:
        raise HTTPException(503, "AI model is not available")
    with _translate_lock:
        out = translate_item(s, llm, item_id, lang)
    if not out:
        raise HTTPException(502, "translation failed")
    return out


@router.get("/api/v1/events/hot", tags=["api"])
def api_hot(request: Request, channel: str | None = None, days: int = 3, limit: int = 20, lang: str | None = None):
    _channel_or_404(request, channel)
    with db(request) as conn:
        events = Q.hot_events(conn, channel, limit=min(limit, 50), days=min(max(days, 1), 14), lang=lang)
    return {"events": [_public_event(e, _base(request)) for e in events]}


def _public_event(e: dict, base: str) -> dict:
    return {"id": e["id"], "channel": e["channel"], "title": e["title"], "heat": e["heat"],
            "source_count": e["source_count"], "item_count": e["item_count"], "sources": e.get("sources", []),
            "first_seen": e["first_seen"], "last_seen": e["last_seen"], "page": f"{base}/event/{e['id']}",
            "lead": Q.public_item(e["lead"], base) if e.get("lead") else None}


@router.get("/api/v1/events/{event_id}", tags=["api"])
def api_event(request: Request, event_id: int, lang: str | None = None):
    with db(request) as conn:
        ev = Q.event(conn, event_id)
        if not ev:
            raise HTTPException(404)
        items = Q.event_items(conn, event_id, lang)
    return {**ev, "items": [Q.public_item(i, _base(request)) for i in items]}


@router.get("/api/v1/opportunities", tags=["api"])
def api_opportunities(request: Request, within_days: int | None = None, open_to_pakistan: bool = False,
                      category: str | None = None, limit: int = 50, lang: str | None = None):
    with db(request) as conn:
        items = Q.open_opportunities(conn, within_days=within_days, limit=min(limit, 200), lang=lang,
                                     open_to_pakistan=open_to_pakistan, category=category)
    return {"items": [Q.public_item(i, _base(request)) for i in items]}


@router.get("/api/v1/search", tags=["api"])
def api_search(request: Request, q: str, channel: str | None = None, limit: int = 30, lang: str | None = None):
    _channel_or_404(request, channel)
    with db(request) as conn:
        items = Q.search(conn, q, channel, limit=min(limit, 100), lang=lang)
    return {"query": q, "items": [Q.public_item(i, _base(request)) for i in items]}


@router.get("/api/v1/digests", tags=["api"])
def api_digests(request: Request, channel: str | None = None, kind: str | None = None, limit: int = 20):
    with db(request) as conn:
        return {"digests": Q.digests(conn, channel, kind, min(limit, 100))}


@router.get("/api/v1/digests/{channel}/{kind}/{slug}", tags=["api"])
def api_digest(request: Request, channel: str, kind: str, slug: str):
    with db(request) as conn:
        d = Q.latest_digest(conn, channel, kind) if slug == "latest" else Q.digest(conn, channel, kind, slug)
    if not d:
        raise HTTPException(404)
    return d


@router.get("/api/v1/stats", tags=["api"])
def api_stats(request: Request):
    with db(request) as conn:
        return Q.stats(conn)


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------
def _rss(title: str, link: str, desc: str, entries: list[dict]) -> Response:
    parts = []
    for e in entries:
        dt = parse_iso(e.get("date"))
        pub = f"<pubDate>{format_datetime(dt)}</pubDate>" if dt else ""
        cat = f"<category>{escape(e['category'])}</category>" if e.get("category") else ""
        parts.append(f"<item><title>{escape(e['title'] or '')}</title><link>{escape(e['link'])}</link>"
                     f"<guid isPermaLink=\"true\">{escape(e['link'])}</guid>{pub}{cat}"
                     f"<description>{escape(e.get('description') or '')}</description></item>")
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel><title>{escape(title)}</title>'
           f"<link>{escape(link)}</link><description>{escape(desc)}</description>{''.join(parts)}</channel></rss>")
    return Response(xml, media_type="application/rss+xml; charset=utf-8")


def _item_entries(items: list[dict], base: str) -> list[dict]:
    out = []
    for i in items:
        desc = i.get("summary") or ""
        if i.get("why_it_matters"):
            desc += f"\n\nWhy it matters: {i['why_it_matters']}"
        if i.get("deadline"):
            desc += f"\n\nDeadline: {i['deadline']}"
        desc += f"\n\nSource: {i.get('source') or ''} — {i['url']}"
        out.append({"title": i.get("title") or i["original_title"], "link": f"{base}/item/{i['id']}",
                    "date": i.get("selected_at") or i.get("discovered_at"), "description": desc,
                    "category": i.get("category")})
    return out


@router.get("/feed.xml", include_in_schema=False)
def feed_all(request: Request):
    s = settings_of(request)
    with db(request) as conn:
        items = Q.latest(conn, None, limit=50)
    return _rss(s.name, s.site_url, s.site.get("tagline", ""), _item_entries(items, s.site_url))


@router.get("/feed/{channel}.xml", include_in_schema=False)
def feed_channel(request: Request, channel: str):
    s = settings_of(request)
    ch = s.channels.get(channel)
    if not ch:
        raise HTTPException(404)
    with db(request) as conn:
        items = Q.latest(conn, channel, limit=50)
    return _rss(f"{s.name} — {ch.name}", f"{s.site_url}/c/{channel}", ch.description, _item_entries(items, s.site_url))


@router.get("/feed/{channel}/daily.xml", include_in_schema=False)
def feed_daily(request: Request, channel: str):
    s = settings_of(request)
    ch = s.channels.get(channel)
    if not ch:
        raise HTTPException(404)
    with db(request) as conn:
        rows = Q.digests(conn, channel, "daily", 20)
    entries = [{"title": d["title"], "link": f"{s.site_url}/briefings/{channel}/{d['kind']}/{d['slug']}",
                "date": d["period_end"], "description": d["intro"] or ""} for d in rows]
    return _rss(f"{s.name} — {ch.name} daily", f"{s.site_url}/briefings?channel={channel}", ch.description, entries)


# ---------------------------------------------------------------------------
# llms.txt, robots, sitemap
# ---------------------------------------------------------------------------
@router.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
def llms_txt(request: Request):
    s = settings_of(request)
    lines = [f"# {s.name}", "", f"> {s.site.get('tagline', '')}", "",
             "Machine-readable access (no key needed):", "",
             f"- [Channels]({s.site_url}/api/v1/channels): list of channels and categories",
             f"- [Latest picks]({s.site_url}/api/v1/items?channel=ai): selected stories, JSON",
             f"- [Hot events]({s.site_url}/api/v1/events/hot): stories ranked by how many sources cover them",
             f"- [Open opportunities]({s.site_url}/api/v1/opportunities?within_days=14): scholarships & jobs with deadlines",
             f"- [Search]({s.site_url}/api/v1/search?q=example)",
             f"- [MCP server]({s.site_url}/mcp): JSON-RPC over HTTP (tools: latest_news, search_news, hot_events, daily_briefing, open_opportunities)",
             f"- [OpenAPI]({s.site_url}/api/openapi.json)", "", "## Channels", ""]
    for c in s.channels.values():
        lines.append(f"- [{c.name}]({s.site_url}/c/{c.key}): {c.description} — RSS: {s.site_url}/feed/{c.key}.xml")
    return "\n".join(lines) + "\n"


@router.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots(request: Request):
    return f"User-agent: *\nDisallow: /admin\nSitemap: {settings_of(request).site_url}/sitemap.xml\n"


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap(request: Request):
    s = settings_of(request)
    urls = [s.site_url + "/", s.site_url + "/hot", s.site_url + "/briefings", s.site_url + "/opportunities"]
    urls += [f"{s.site_url}/c/{k}" for k in s.channels]
    with db(request) as conn:
        urls += [f"{s.site_url}/item/{r['id']}" for r in conn.execute(
            "SELECT id FROM items WHERE selected=1 ORDER BY selected_at DESC LIMIT 2000")]
        urls += [f"{s.site_url}/briefings/{r['channel']}/{r['kind']}/{r['slug']}" for r in conn.execute(
            "SELECT channel, kind, slug FROM digests ORDER BY period_end DESC LIMIT 500")]
    body = "".join(f"<url><loc>{escape(u)}</loc></url>" for u in urls)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"{body}</urlset>", media_type="application/xml")


# ---------------------------------------------------------------------------
# MCP (Model Context Protocol) — lets AI agents use the site as a tool
# ---------------------------------------------------------------------------
MCP_TOOLS = [
    {"name": "latest_news", "description": "Latest selected stories. Optional channel filter.",
     "inputSchema": {"type": "object", "properties": {
         "channel": {"type": "string", "description": "Channel key, e.g. ai, opportunities, pakistan"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}}},
    {"name": "search_news", "description": "Full-text search across all analysed stories.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string"}, "channel": {"type": "string"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}}},
    {"name": "hot_events", "description": "Events ranked by heat (how many independent sources cover them).",
     "inputSchema": {"type": "object", "properties": {
         "channel": {"type": "string"}, "days": {"type": "integer", "minimum": 1, "maximum": 14, "default": 3},
         "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10}}}},
    {"name": "daily_briefing", "description": "The most recent daily briefing for a channel.",
     "inputSchema": {"type": "object", "required": ["channel"], "properties": {"channel": {"type": "string"}}}},
    {"name": "open_opportunities", "description": "Open scholarships, jobs and fellowships sorted by deadline.",
     "inputSchema": {"type": "object", "properties": {
         "within_days": {"type": "integer", "minimum": 1, "maximum": 365},
         "open_to_pakistan": {"type": "boolean", "default": False},
         "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 15}}}},
]


def _md_items(items: list[dict]) -> str:
    if not items:
        return "No results."
    lines = []
    for i in items:
        line = f"- **{i['title']}** ({i.get('source') or ''}"
        if i.get("opportunity", {}).get("deadline"):
            line += f", deadline {i['opportunity']['deadline']}"
        line += f")\n  {truncate(i.get('summary') or '', 300)}\n  {i['page']}"
        lines.append(line)
    return "\n".join(lines)


def _mcp_call(request: Request, name: str, args: dict) -> tuple[str, dict]:
    base = _base(request)
    channel = args.get("channel") or None
    if channel and channel not in settings_of(request).channels:
        raise ValueError(f"unknown channel '{channel}'. Valid: {', '.join(settings_of(request).channels)}")
    limit = max(1, min(int(args.get("limit", 10)), 50))
    with db(request) as conn:
        if name == "latest_news":
            items = [Q.public_item(i, base) for i in Q.latest(conn, channel, limit=limit)]
            return _md_items(items), {"items": items}
        if name == "search_news":
            q = str(args.get("query", "")).strip()
            if not q:
                raise ValueError("query is required")
            items = [Q.public_item(i, base) for i in Q.search(conn, q, channel, limit=limit)]
            return _md_items(items), {"items": items}
        if name == "hot_events":
            days = max(1, min(int(args.get("days", 3)), 14))
            events = [_public_event(e, base) for e in Q.hot_events(conn, channel, limit=min(limit, 30), days=days)]
            text = "\n".join(f"- **{e['title']}** — heat {e['heat']}, {e['source_count']} sources\n  {e['page']}"
                             for e in events) or "No hot events."
            return text, {"events": events}
        if name == "daily_briefing":
            if not channel:
                raise ValueError("channel is required")
            d = Q.latest_digest(conn, channel)
            if not d:
                return "No briefing yet.", {}
            body = d["body"]
            entries = ([body["lead"]] if body.get("lead") else []) + body.get("main", [])
            text = f"# {d['title']}\n\n{d.get('intro') or ''}\n\n" + "\n".join(
                f"{n}. **{e['title']}** — {truncate(e.get('summary') or '', 240)} ({e.get('source')})"
                for n, e in enumerate(entries, 1))
            return text, {"digest": d}
        if name == "open_opportunities":
            within = args.get("within_days")
            items = [Q.public_item(i, base) for i in Q.open_opportunities(
                conn, within_days=int(within) if within else None, limit=limit,
                open_to_pakistan=bool(args.get("open_to_pakistan")))]
            return _md_items(items), {"items": items}
    raise ValueError(f"unknown tool '{name}'")


def _rpc_result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _rpc_error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _handle_rpc(request: Request, msg: dict) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _rpc_error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
    method, id_, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if id_ is None:          # notification (e.g. notifications/initialized)
        return None
    s = settings_of(request)
    if method == "initialize":
        return _rpc_result(id_, {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {"name": s.name.lower().replace(" ", "-"), "version": "0.1.0"},
                                 "instructions": f"News and opportunities from {s.name}. Channels: {', '.join(s.channels)}."})
    if method == "ping":
        return _rpc_result(id_, {})
    if method == "tools/list":
        return _rpc_result(id_, {"tools": MCP_TOOLS})
    if method == "tools/call":
        try:
            text, data = _mcp_call(request, params.get("name", ""), params.get("arguments") or {})
            return _rpc_result(id_, {"content": [{"type": "text", "text": text}], "structuredContent": data,
                                     "isError": False})
        except (ValueError, TypeError) as exc:
            return _rpc_result(id_, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
    return _rpc_error(id_, -32601, f"method not found: {method}")


@router.post("/mcp", include_in_schema=False)
async def mcp(request: Request):
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(_rpc_error(None, -32700, "parse error"), status_code=400)
    if isinstance(payload, list):
        replies = [r for r in (_handle_rpc(request, m) for m in payload) if r is not None]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply = _handle_rpc(request, payload)
    return JSONResponse(reply) if reply is not None else Response(status_code=202)


@router.get("/mcp", include_in_schema=False)
def mcp_get():
    return Response(status_code=405, headers={"Allow": "POST"})
