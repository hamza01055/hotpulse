"""Shared helpers for the web layer: templates, language detection, formatting filters."""
from __future__ import annotations

import hashlib
import hmac
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from ..db import session
from ..text import parse_iso
from .i18n import t as _t

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def settings_of(request: Request):
    return request.app.state.settings


def db(request: Request):
    return session(settings_of(request).db_path)


def pick_lang(request: Request) -> str:
    s = settings_of(request)
    q = request.query_params.get("lang")
    if q and q in s.languages:
        return q
    c = request.cookies.get("lang")
    if c and c in s.languages:
        return c
    accept = request.headers.get("accept-language", "")
    for part in accept.split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in s.languages:
            return code
    return s.primary_language


def admin_cookie_value(token: str) -> str:
    return hmac.new(token.encode(), b"hotpulse-admin", hashlib.sha256).hexdigest()


def is_admin(request: Request) -> bool:
    s = settings_of(request)
    if not s.admin_token or s.admin_token == "change-me" and not request.app.state.allow_default_admin:
        return False
    supplied = request.cookies.get("hp_admin", "")
    return hmac.compare_digest(supplied, admin_cookie_value(s.admin_token))


# ---------------------------------------------------------------------------
# Jinja filters
# ---------------------------------------------------------------------------
def timeago(value: str | None) -> str:
    dt = parse_iso(value)
    if not dt:
        return ""
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 90:
        return "just now"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)}m ago"
    hours = mins / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 8:
        return f"{int(days)}d ago"
    return dt.strftime("%d %b %Y")


def days_left(deadline: str | None) -> int | None:
    if not deadline:
        return None
    try:
        return (date.fromisoformat(deadline) - date.today()).days
    except ValueError:
        return None


def nice_date(value: str | None, tz: str = "UTC", fmt: str = "%d %b %Y") -> str:
    if not value:
        return ""
    if len(value) == 10:
        try:
            return date.fromisoformat(value).strftime(fmt)
        except ValueError:
            return value
    dt = parse_iso(value)
    if not dt:
        return value
    try:
        dt = dt.astimezone(ZoneInfo(tz))
    except Exception:  # noqa: BLE001
        pass
    return dt.strftime(fmt)


def host(url: str | None) -> str:
    from urllib.parse import urlsplit
    h = urlsplit(url or "").hostname or ""
    return h[4:] if h.startswith("www.") else h


def nl2p(text: str | None) -> Markup:
    if not text:
        return Markup("")
    paras = [p.strip() for p in str(text).split("\n") if p.strip()]
    return Markup("".join(f"<p>{escape(p)}</p>" for p in paras))


templates.env.filters.update(timeago=timeago, days_left=days_left, nice_date=nice_date, host=host, nl2p=nl2p)
templates.env.globals.update(now=lambda: datetime.now(timezone.utc))


def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200):
    s = settings_of(request)
    lang = pick_lang(request)
    with db(request) as conn:
        from ..demo import has_demo
        demo = has_demo(conn)
    base = {
        "request": request, "s": s, "site": s.site, "lang": lang, "rtl": s.is_rtl(lang),
        "t": lambda key: _t(lang, key), "channels": s.channels, "demo": demo, "is_admin": is_admin(request),
        "primary": s.primary_language, "path": request.url.path, "ai_ready": request.app.state.ai_ready(),
    }
    base.update(ctx or {})
    resp = templates.TemplateResponse(request, name, base, status_code=status_code)
    if request.query_params.get("lang") in s.languages:
        resp.set_cookie("lang", lang, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp
