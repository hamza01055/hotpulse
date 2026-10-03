import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from hotpulse.db import session
from hotpulse.digest import build_today_preview
from hotpulse.web.app import create_app


@pytest.fixture
def client(demo_settings):
    for key in demo_settings.channels:
        build_today_preview(demo_settings, None, key)
    return TestClient(create_app(demo_settings))


PAGES = ["/", "/c/ai", "/c/ai?view=all", "/c/opportunities?pk=1", "/c/pakistan?cat=startups", "/hot", "/hot?days=7",
         "/opportunities", "/opportunities?within=7&pk=1", "/item/1", "/event/1", "/briefings", "/search?q=nimbus",
         "/saved", "/about", "/developers", "/?lang=ur", "/c/ai?lang=ar", "/briefings/ai/latest"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200, path
    assert "<html" in r.text


def test_rtl_and_language_cookie(client):
    r = client.get("/?lang=ur")
    assert 'dir="rtl"' in r.text and r.cookies.get("lang") == "ur"
    assert 'dir="rtl"' in client.get("/").text, "cookie remembers the language"


def test_404(client):
    assert client.get("/item/99999").status_code == 404
    assert client.get("/c/nope").status_code == 404
    assert client.get("/api/v1/items/99999").json() == {"error": "not found"}


def test_html_is_escaped(client, demo_settings):
    with session(demo_settings.db_path) as conn:
        conn.execute("UPDATE items SET title='<script>alert(1)</script>' WHERE id=1")
    r = client.get("/item/1")
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text


def test_api(client):
    ch = client.get("/api/v1/channels").json()
    assert [c["key"] for c in ch["channels"]] == ["ai", "opportunities", "pakistan"]
    items = client.get("/api/v1/items?channel=ai").json()["items"]
    assert items and {"id", "title", "summary", "url", "page", "source"} <= set(items[0])
    hot = client.get("/api/v1/events/hot").json()["events"]
    assert hot[0]["source_count"] >= hot[-1]["source_count"] or hot[0]["heat"] >= hot[-1]["heat"]
    opps = client.get("/api/v1/opportunities?open_to_pakistan=true").json()["items"]
    assert opps and all(o["opportunity"]["deadline"] for o in opps)
    assert client.get("/api/v1/search?q=paynest").json()["items"]
    assert client.get("/api/v1/digests/ai/daily/latest").status_code == 200
    assert client.get("/api/v1/items?channel=nope").status_code == 404


def test_translate_without_model(client):
    assert client.post("/api/v1/items/1/translate?lang=ur").status_code == 503
    assert client.post("/api/v1/items/1/translate?lang=xx").status_code == 400


def test_feeds_are_valid_xml(client):
    for path in ["/feed.xml", "/feed/ai.xml", "/feed/opportunities/daily.xml", "/sitemap.xml"]:
        r = client.get(path)
        assert r.status_code == 200
        ET.fromstring(r.content)
    assert "https://hp.test/item/" in client.get("/feed/ai.xml").text
    assert "/mcp" in client.get("/llms.txt").text


def rpc(client, method, params=None, id_=1):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}).json()


def test_mcp(client):
    init = rpc(client, "initialize", {"protocolVersion": "2025-06-18"})
    assert init["result"]["capabilities"]["tools"] is not None
    assert client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    tools = {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}
    assert tools == {"latest_news", "search_news", "hot_events", "daily_briefing", "open_opportunities"}
    res = rpc(client, "tools/call", {"name": "search_news", "arguments": {"query": "Nimbus"}})["result"]
    assert not res["isError"] and "Nimbus" in res["content"][0]["text"]
    res = rpc(client, "tools/call", {"name": "daily_briefing", "arguments": {"channel": "pakistan"}})["result"]
    assert "Pakistan Tech Daily" in res["content"][0]["text"]
    bad = rpc(client, "tools/call", {"name": "latest_news", "arguments": {"channel": "nope"}})["result"]
    assert bad["isError"]
    assert rpc(client, "nope/method")["error"]["code"] == -32601
    assert client.post("/mcp", content=b"not json", headers={"content-type": "application/json"}).status_code == 400


def test_admin_requires_login(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert client.post("/admin/login", data={"token": "wrong"}, follow_redirects=False).headers["location"] == "/admin/login?error=1"
    r = client.post("/admin/login", data={"token": "secret-token"}, follow_redirects=False)
    assert r.headers["location"] == "/admin"
    for path in ["/admin", "/admin/sources", "/admin/items", "/admin/social", "/admin/items?status=blocked"]:
        assert client.get(path).status_code == 200, path


def test_admin_actions(client, demo_settings):
    client.post("/admin/login", data={"token": "secret-token"})
    client.post("/admin/items/1/select", data={"value": 0})
    with session(demo_settings.db_path) as conn:
        assert conn.execute("SELECT selected FROM items WHERE id=1").fetchone()[0] == 0
    r = client.post("/admin/sources/add", data={"channel": "ai", "name": "My feed", "url": "https://my.test/feed", "tier": "T1"})
    assert r.status_code == 200
    with session(demo_settings.db_path) as conn:
        assert conn.execute("SELECT tier FROM sources WHERE url='https://my.test/feed'").fetchone()[0] == "T1"
    assert "My feed" in client.get("/admin/sources").text
    assert client.post("/admin/social/generate").status_code == 200


def test_default_token_refused_when_not_local(demo_settings, monkeypatch):
    demo_settings.admin_token = "change-me"
    c = TestClient(create_app(demo_settings, allow_default_admin=False))
    r = c.post("/admin/login", data={"token": "change-me"}, follow_redirects=False)
    assert r.headers["location"] == "/admin/login?error=1"
