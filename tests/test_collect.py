import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from hotpulse.collect import collect, parse_remoteok, parse_rss
from hotpulse.db import session
from conftest import offline_fetcher, rss


def _now(h=0):
    return format_datetime(datetime.now(timezone.utc) - timedelta(hours=h))


def _add_source(settings, url, channel="ai", tier="T2", kind="rss", last_ok=True):
    with session(settings.db_path) as conn:
        conn.execute("UPDATE sources SET enabled=0 WHERE from_config=1")
        conn.execute("INSERT INTO sources(channel, name, url, kind, tier, enabled, from_config, last_ok_at) "
                     "VALUES (?,?,?,?,?,1,0,?)", (channel, url, url, kind, tier, "2026-01-01T00:00:00+00:00" if last_ok else None))


def test_parse_rss_basic():
    entries = parse_rss(rss([("Hello &amp; world", "https://a.com/1", "&lt;p&gt;Body text&lt;/p&gt;", _now())]))
    assert entries[0]["title"] == "Hello & world"
    assert entries[0]["content"] == "Body text"
    assert entries[0]["published"]


def test_dedupe_by_url_and_title_and_archive_old(settings):
    _add_source(settings, "https://feed.test/a")
    _add_source(settings, "https://feed.test/b")
    feeds = {
        "https://feed.test/a": rss([("Big launch today", "https://site.com/x?utm_source=rss", "body", _now(1)),
                                    ("Ancient news", "https://site.com/old", "body", _now(200))]),
        "https://feed.test/b": rss([("Big launch today", "https://mirror.com/copy", "body", _now(1)),   # same headline
                                    ("Another", "https://www.site.com/x", "body", _now(1))]),          # same URL
    }
    stats = collect(settings, fetcher=offline_fetcher(feeds))
    assert stats["ok"] == 2 and stats["new_items"] == 2
    with session(settings.db_path) as conn:
        rows = {r["original_title"]: r["archived"] for r in conn.execute("SELECT * FROM items")}
    assert rows == {"Big launch today": 0, "Ancient news": 1}


def test_first_fetch_archives_backlog(settings):
    _add_source(settings, "https://feed.test/new", last_ok=False)
    feeds = {"https://feed.test/new": rss([("Fresh", "https://n.com/1", "b", _now(2)), ("Day old", "https://n.com/2", "b", _now(30))])}
    collect(settings, fetcher=offline_fetcher(feeds))
    with session(settings.db_path) as conn:
        rows = {r["original_title"]: r["archived"] for r in conn.execute("SELECT * FROM items")}
    assert rows == {"Fresh": 0, "Day old": 1}


def test_broken_source_does_not_stop_others(settings):
    _add_source(settings, "https://feed.test/ok")
    _add_source(settings, "https://feed.test/broken")
    feeds = {"https://feed.test/ok": rss([("Works", "https://ok.com/1", "b", _now(1))])}
    stats = collect(settings, fetcher=offline_fetcher(feeds))
    assert stats["ok"] == 1 and stats["failed"] == 1
    with session(settings.db_path) as conn:
        bad = conn.execute("SELECT * FROM sources WHERE url='https://feed.test/broken'").fetchone()
    assert bad["fail_count"] == 1 and "offline" in bad["last_error"]


def test_parse_remoteok():
    raw = json.dumps([{"legal": "notice"}, {"position": "Backend Engineer", "company": "Acme", "url": "https://r.com/1",
                       "date": "2026-10-01T10:00:00+00:00", "tags": ["python"], "salary_min": 50000, "salary_max": 70000,
                       "description": "<p>Join us</p>", "location": "Worldwide"}]).encode()
    out = parse_remoteok(raw)
    assert len(out) == 1
    assert out[0]["title"] == "Backend Engineer at Acme"
    assert "$50,000" in out[0]["content"] and "Join us" in out[0]["content"]
