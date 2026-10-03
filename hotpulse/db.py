"""SQLite storage. One file, no server. WAL mode lets the web app read while the worker writes."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'rss',
  tier TEXT NOT NULL DEFAULT 'T2',
  lang TEXT NOT NULL DEFAULT 'en',
  enabled INTEGER NOT NULL DEFAULT 1,
  from_config INTEGER NOT NULL DEFAULT 1,
  last_fetched_at TEXT,
  last_ok_at TEXT,
  last_error TEXT,
  fail_count INTEGER NOT NULL DEFAULT 0,
  items_total INTEGER NOT NULL DEFAULT 0,
  UNIQUE(channel, url)
);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,
  source_id INTEGER REFERENCES sources(id),
  url TEXT NOT NULL,
  canonical_url TEXT NOT NULL UNIQUE,
  title_hash TEXT,
  original_title TEXT NOT NULL,
  author TEXT,
  image_url TEXT,
  published_at TEXT,
  discovered_at TEXT NOT NULL,
  content TEXT,
  lang TEXT,
  archived INTEGER NOT NULL DEFAULT 0,      -- discovered too old: kept but never in "today"
  status TEXT NOT NULL DEFAULT 'new',       -- new | analyzed | blocked | failed
  attempts INTEGER NOT NULL DEFAULT 0,
  prefilter TEXT,
  score1 INTEGER,
  score2 INTEGER,
  score INTEGER,
  score_reason TEXT,
  selected INTEGER NOT NULL DEFAULT 0,
  selected_at TEXT,
  category TEXT,
  tags TEXT,                                 -- json list
  entities TEXT,                             -- json list
  story_type TEXT,
  title TEXT,                                -- written headline (primary language)
  summary TEXT,
  why_it_matters TEXT,
  facts TEXT,                                -- json object (opportunity fields etc.)
  deadline TEXT,                             -- YYYY-MM-DD for opportunities
  event_id INTEGER REFERENCES events(id),
  analyzed_at TEXT,
  analyzed_by TEXT,                          -- model name or 'heuristic'
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_channel_time ON items(channel, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_event ON items(event_id);
CREATE INDEX IF NOT EXISTS idx_items_title_hash ON items(title_hash);
CREATE INDEX IF NOT EXISTS idx_items_deadline ON items(deadline);

CREATE TABLE IF NOT EXISTS translations (
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  lang TEXT NOT NULL,
  title TEXT, summary TEXT, why_it_matters TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(item_id, lang)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,
  lead_item_id INTEGER,
  title TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  item_count INTEGER NOT NULL DEFAULT 0,
  source_count INTEGER NOT NULL DEFAULT 0,
  max_score INTEGER NOT NULL DEFAULT 0,
  heat REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_heat ON events(channel, heat DESC);

CREATE TABLE IF NOT EXISTS digests (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,
  kind TEXT NOT NULL,                        -- daily | weekly
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  slug TEXT NOT NULL,                        -- e.g. 2026-10-04 or 2026-W40
  title TEXT NOT NULL,
  intro TEXT,
  body TEXT NOT NULL,                        -- json
  created_at TEXT NOT NULL,
  UNIQUE(channel, kind, slug)
);

CREATE TABLE IF NOT EXISTS social_posts (
  id INTEGER PRIMARY KEY,
  item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,                    -- x | threads
  lang TEXT NOT NULL DEFAULT 'en',
  text TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(item_id, platform, lang)
);

CREATE TABLE IF NOT EXISTS llm_cache (
  key TEXT PRIMARY KEY,
  model TEXT,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  stats TEXT,
  error TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, summary, original_title, content, entities, tokenize='unicode61 remove_diacritics 2'
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | str) -> sqlite3.Connection:
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: Path | str) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def session(path: Path | str) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def jloads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def jdumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("tags", "entities"):
        if key in d:
            d[key] = jloads(d[key], [])
    if "facts" in d:
        d["facts"] = jloads(d["facts"], {})
    if "body" in d:
        d["body"] = jloads(d["body"], {})
    return d


def index_item(conn: sqlite3.Connection, item_id: int) -> None:
    """Refresh the full-text search row for one item."""
    row = conn.execute("SELECT title, summary, original_title, content, entities FROM items WHERE id=?",
                       (item_id,)).fetchone()
    if not row:
        return
    conn.execute("DELETE FROM items_fts WHERE rowid=?", (item_id,))
    conn.execute("INSERT INTO items_fts(rowid, title, summary, original_title, content, entities) VALUES (?,?,?,?,?,?)",
                 (item_id, row["title"] or "", row["summary"] or "", row["original_title"] or "",
                  (row["content"] or "")[:4000], " ".join(jloads(row["entities"], []))))


def sync_sources(conn: sqlite3.Connection, channels: dict) -> None:
    """Make the sources table match the YAML config (admin-added sources are kept)."""
    seen = set()
    for ch in channels.values():
        for s in ch.sources:
            seen.add((ch.key, s.url))
            conn.execute(
                """INSERT INTO sources(channel, name, url, kind, tier, lang, enabled, from_config)
                   VALUES (?,?,?,?,?,?,?,1)
                   ON CONFLICT(channel, url) DO UPDATE SET name=excluded.name, kind=excluded.kind,
                     tier=excluded.tier, lang=excluded.lang, from_config=1""",
                (ch.key, s.name, s.url, s.kind, s.tier, s.lang, 1 if s.enabled else 0))
    for row in conn.execute("SELECT id, channel, url FROM sources WHERE from_config=1").fetchall():
        if (row["channel"], row["url"]) not in seen:
            conn.execute("UPDATE sources SET enabled=0, from_config=0 WHERE id=?", (row["id"],))
