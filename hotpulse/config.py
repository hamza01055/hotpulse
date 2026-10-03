"""Loads site settings, channel definitions and prompt files from the config folder."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

TIERS = ("T1", "T2", "T3")


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader so users don't need python-dotenv. Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Category:
    key: str
    name: str
    keywords: list[str] = field(default_factory=list)


@dataclass
class SourceDef:
    name: str
    url: str
    tier: str = "T2"
    kind: str = "rss"          # rss | json | remoteok
    lang: str = "en"
    enabled: bool = True


@dataclass
class Channel:
    key: str
    name: str
    description: str
    kind: str = "news"
    icon: str = "•"
    thresholds: dict[str, int] = field(default_factory=lambda: {"T1": 60, "T2": 68, "T3": 74})
    rubric: str = ""
    keywords: list[str] = field(default_factory=list)
    block_keywords: list[str] = field(default_factory=list)
    categories: list[Category] = field(default_factory=list)
    sources: list[SourceDef] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)

    @property
    def is_opportunities(self) -> bool:
        return self.kind == "opportunities"

    def threshold(self, tier: str) -> int:
        return int(self.thresholds.get(tier, self.thresholds.get("T2", 65)))

    def category_name(self, key: str | None) -> str:
        for c in self.categories:
            if c.key == key:
                return c.name
        return (key or "Other").replace("-", " ").title()


@dataclass
class Settings:
    root: Path
    db_path: Path
    site: dict[str, Any]
    channels: dict[str, Channel]
    ollama_url: str
    ollama_model: str
    ollama_embed_model: str
    llm_mode: str
    admin_token: str
    site_url: str

    # convenience accessors -------------------------------------------------
    @property
    def name(self) -> str:
        return self.site.get("name", "HotPulse")

    @property
    def primary_language(self) -> str:
        return self.site.get("primary_language", "en")

    @property
    def languages(self) -> dict[str, dict]:
        return self.site.get("languages", {"en": {"name": "English"}})

    @property
    def timezone(self) -> str:
        return self.site.get("timezone", "UTC")

    def pipeline(self, key: str, default: Any) -> Any:
        return self.site.get("pipeline", {}).get(key, default)

    def schedule(self, key: str, default: Any) -> Any:
        return self.site.get("schedule", {}).get(key, default)

    def language_name(self, code: str) -> str:
        names = {"en": "English", "ur": "Urdu", "ar": "Arabic", "hi": "Hindi", "zh": "Simplified Chinese",
                 "es": "Spanish", "fr": "French", "tr": "Turkish", "id": "Indonesian", "bn": "Bengali",
                 "de": "German", "pt": "Portuguese", "ru": "Russian", "fa": "Persian", "ja": "Japanese"}
        return names.get(code, self.languages.get(code, {}).get("name", code))

    def is_rtl(self, code: str) -> bool:
        return bool(self.languages.get(code, {}).get("rtl"))

    def prompt(self, name: str) -> str:
        return load_prompt(str(self.root / "prompts"), name)


@lru_cache(maxsize=64)
def load_prompt(folder: str, name: str) -> str:
    return (Path(folder) / f"{name}.md").read_text(encoding="utf-8").strip()


def render(template: str, **values: Any) -> str:
    """Replace {{key}} placeholders; unknown keys become empty strings."""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), "")), template)


def _channel_from_yaml(data: dict) -> Channel:
    cats = [Category(key=c["key"], name=c.get("name", c["key"]),
                     keywords=[str(k).lower() for k in c.get("keywords", [])])
            for c in data.get("categories", [])]
    sources = []
    for s in data.get("sources", []):
        tier = str(s.get("tier", "T2")).upper()
        if tier not in TIERS:
            tier = "T2"
        sources.append(SourceDef(name=s["name"], url=s["url"], tier=tier, kind=s.get("kind", "rss"),
                                 lang=s.get("lang", "en"), enabled=s.get("enabled", True)))
    return Channel(
        key=data["key"], name=data.get("name", data["key"]), description=data.get("description", ""),
        kind=data.get("kind", "news"), icon=data.get("icon", "•"),
        thresholds={k.upper(): int(v) for k, v in (data.get("thresholds") or {}).items()} or {"T2": 65},
        rubric=data.get("rubric", ""),
        keywords=[str(k).lower() for k in data.get("keywords", [])],
        block_keywords=[str(k).lower() for k in data.get("block_keywords", [])],
        categories=cats, sources=sources, hashtags=data.get("hashtags", []),
    )


def load_settings(config_dir: str | os.PathLike | None = None, db_path: str | None = None) -> Settings:
    _load_dotenv(Path.cwd() / ".env")
    root = Path(config_dir or os.environ.get("HOTPULSE_CONFIG", "config")).resolve()
    if not (root / "site.yaml").exists():
        raise FileNotFoundError(f"site.yaml not found in {root}. Set HOTPULSE_CONFIG or run from the project folder.")
    site = yaml.safe_load((root / "site.yaml").read_text(encoding="utf-8")) or {}
    channels: dict[str, Channel] = {}
    order = site.get("channels") or [p.stem for p in sorted((root / "channels").glob("*.yaml"))]
    for key in order:
        path = root / "channels" / f"{key}.yaml"
        if path.exists():
            ch = _channel_from_yaml(yaml.safe_load(path.read_text(encoding="utf-8")))
            channels[ch.key] = ch
    db = Path(db_path or os.environ.get("HOTPULSE_DB", "data/hotpulse.db"))
    return Settings(
        root=root, db_path=db, site=site, channels=channels,
        ollama_url=os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        ollama_model=os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"),
        ollama_embed_model=os.environ.get("OLLAMA_EMBED_MODEL", ""),
        llm_mode=os.environ.get("LLM_MODE", "auto").lower(),
        admin_token=os.environ.get("ADMIN_TOKEN", "change-me"),
        site_url=os.environ.get("SITE_URL", "http://localhost:8000").rstrip("/"),
    )
