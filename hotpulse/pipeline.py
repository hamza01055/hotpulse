"""One full cycle: collect -> analyse -> group -> heat -> translate -> digests. Plus a simple scheduler."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from .analyze import analyze_pending, auto_translate
from .collect import collect, http_fetch
from .db import init_db, jdumps, session, sync_sources, utcnow
from .digest import ensure_digests
from .events import group_pending, refresh_heat
from .llm import get_llm

log = logging.getLogger("hotpulse.pipeline")
_run_lock = threading.Lock()


def prepare(settings) -> None:
    init_db(settings.db_path)
    with session(settings.db_path) as conn:
        sync_sources(conn, settings.channels)


def run_cycle(settings, *, llm="auto", fetcher=http_fetch, collect_sources: bool = True,
              channel: str | None = None, url_prefix: str | None = None) -> dict:
    """Run everything once. Returns stats. Concurrent calls are skipped, not stacked."""
    if not _run_lock.acquire(blocking=False):
        return {"skipped": "a cycle is already running"}
    try:
        prepare(settings)
        if llm == "auto":
            llm = get_llm(settings)
        with session(settings.db_path) as conn:
            run_id = conn.execute("INSERT INTO runs(kind, started_at) VALUES ('cycle', ?)", (utcnow(),)).lastrowid
        stats: dict = {"mode": llm.name if llm else "heuristic"}
        error = None
        try:
            if collect_sources:
                stats["collect"] = collect(settings, channel=channel, fetcher=fetcher, url_prefix=url_prefix)
            stats["analyze"] = analyze_pending(settings, llm, fetcher=fetcher)
            stats["group"] = group_pending(settings, llm)
            stats["heat_refreshed"] = refresh_heat(settings)
            stats["translated"] = auto_translate(settings, llm)
            stats["digests"] = ensure_digests(settings, llm)
            cleanup(settings)
        except Exception as exc:  # noqa: BLE001
            log.exception("cycle failed")
            error = f"{exc.__class__.__name__}: {exc}"
            stats["error"] = error
        with session(settings.db_path) as conn:
            conn.execute("UPDATE runs SET finished_at=?, stats=?, error=? WHERE id=?",
                         (utcnow(), jdumps(stats), error, run_id))
        return stats
    finally:
        _run_lock.release()


def cleanup(settings, keep_days: int = 120) -> None:
    """Drop old non-selected items and stale cache so the SQLite file stays small."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    with session(settings.db_path) as conn:
        old = [r["id"] for r in conn.execute(
            "SELECT id FROM items WHERE selected=0 AND discovered_at < ?", (cutoff,))]
        for item_id in old:
            conn.execute("DELETE FROM items_fts WHERE rowid=?", (item_id,))
        if old:
            conn.execute(f"DELETE FROM items WHERE id IN ({','.join('?' * len(old))})", old)
        conn.execute("DELETE FROM llm_cache WHERE created_at < ?", (cutoff,))
        conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
        conn.execute("DELETE FROM events WHERE id NOT IN (SELECT DISTINCT event_id FROM items WHERE event_id IS NOT NULL)")


class Scheduler(threading.Thread):
    """Background loop used by `hotpulse serve`. Runs a cycle every N minutes."""

    def __init__(self, settings):
        super().__init__(daemon=True, name="hotpulse-scheduler")
        self.settings = settings
        self.stop_event = threading.Event()
        self.last_stats: dict | None = None
        self.next_run_at: datetime | None = None

    def run(self) -> None:
        every = max(5, int(self.settings.schedule("collect_every_minutes", 30))) * 60
        time.sleep(3)
        while not self.stop_event.is_set():
            started = time.time()
            try:
                self.last_stats = run_cycle(self.settings)
                log.info("cycle done: %s", self.last_stats)
            except Exception:  # noqa: BLE001
                log.exception("scheduler cycle crashed")
            wait = max(30, every - (time.time() - started))
            self.next_run_at = datetime.now(timezone.utc) + timedelta(seconds=wait)
            self.stop_event.wait(wait)

    def stop(self) -> None:
        self.stop_event.set()
