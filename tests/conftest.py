import shutil
from pathlib import Path

import pytest

from hotpulse.config import load_settings
from hotpulse.demo import DEMO_PREFIX, demo_feeds, install_demo_sources
from hotpulse.pipeline import prepare, run_cycle

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    monkeypatch.setenv("LLM_MODE", "off")
    monkeypatch.setenv("ADMIN_TOKEN", "secret-token")
    monkeypatch.setenv("SITE_URL", "https://hp.test")
    monkeypatch.chdir(tmp_path)          # no stray .env from the project
    s = load_settings(cfg, str(tmp_path / "test.db"))
    prepare(s)
    return s


def offline_fetcher(feeds: dict):
    def fetch(url: str) -> bytes:
        if url in feeds:
            return feeds[url]
        raise RuntimeError(f"offline: {url}")
    return fetch


@pytest.fixture
def demo_settings(settings):
    install_demo_sources(settings)
    run_cycle(settings, llm=None, fetcher=offline_fetcher(demo_feeds()), url_prefix=DEMO_PREFIX)
    return settings


def rss(entries):
    """entries: list of (title, link, description, rfc822_date)"""
    items = "".join(f"<item><title>{t}</title><link>{l}</link><description>{d}</description><pubDate>{p}</pubDate></item>"
                    for t, l, d, p in entries)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{items}</channel></rss>'.encode()
