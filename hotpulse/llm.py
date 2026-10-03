"""Local AI via Ollama, with a response cache so restarts never pay twice for the same work.

If Ollama is not running (or LLM_MODE=off) `get_llm` returns None and the pipeline
falls back to transparent keyword heuristics, so the site still works with zero setup.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from typing import Any, Callable, Protocol

import httpx

from .db import session, utcnow

log = logging.getLogger("hotpulse.llm")


class LLM(Protocol):
    name: str

    def chat_json(self, system: str, user: str, *, temperature: float = 0.2, seed: int = 0,
                  cache: bool = True) -> dict | None: ...

    def embed(self, texts: list[str]) -> list[list[float]] | None: ...


def extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model reply (handles ```json fences and chatter)."""
    if not text:
        return None
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        val = json.loads(text)
        return val if isinstance(val, dict) else None
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        val = json.loads(text[start:i + 1])
                        return val if isinstance(val, dict) else None
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


class OllamaLLM:
    def __init__(self, url: str, model: str, db_path, embed_model: str = "", timeout: float = 180.0,
                 max_parallel: int = 1):
        self.url = url.rstrip("/")
        self.model = model
        self.name = f"ollama:{model}"
        self.embed_model = embed_model
        self.db_path = db_path
        self.client = httpx.Client(timeout=timeout)
        self._sem = threading.Semaphore(max_parallel)   # local GPUs prefer one request at a time
        self.calls = 0
        self.cache_hits = 0
        self.failures = 0

    # -- cache ---------------------------------------------------------------
    def _key(self, *parts: Any) -> str:
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def _cache_get(self, key: str) -> dict | None:
        with session(self.db_path) as conn:
            row = conn.execute("SELECT response FROM llm_cache WHERE key=?", (key,)).fetchone()
        return json.loads(row["response"]) if row else None

    def _cache_put(self, key: str, value: dict) -> None:
        with session(self.db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO llm_cache(key, model, response, created_at) VALUES (?,?,?,?)",
                         (key, self.model, json.dumps(value, ensure_ascii=False), utcnow()))

    # -- calls ---------------------------------------------------------------
    def chat_json(self, system: str, user: str, *, temperature: float = 0.2, seed: int = 0,
                  cache: bool = True) -> dict | None:
        key = self._key(self.model, system, user, temperature, seed)
        if cache:
            hit = self._cache_get(key)
            if hit is not None:
                self.cache_hits += 1
                return hit
        payload = {
            "model": self.model, "stream": False, "format": "json",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": temperature, "seed": seed, "num_ctx": 8192},
        }
        for attempt in range(2):
            try:
                with self._sem:
                    self.calls += 1
                    r = self.client.post(f"{self.url}/api/chat", json=payload)
                r.raise_for_status()
                data = extract_json(r.json().get("message", {}).get("content", ""))
                if data is not None:
                    if cache:
                        self._cache_put(key, data)
                    return data
                log.warning("model returned non-JSON (attempt %s)", attempt + 1)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("ollama call failed (attempt %s): %s", attempt + 1, exc)
                time.sleep(1.5)
        self.failures += 1
        return None

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.embed_model or not texts:
            return None
        try:
            r = self.client.post(f"{self.url}/api/embed", json={"model": self.embed_model, "input": texts})
            r.raise_for_status()
            vecs = r.json().get("embeddings")
            return vecs if vecs and len(vecs) == len(texts) else None
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("embedding failed: %s", exc)
            return None


class FakeLLM:
    """Test double: a function decides the reply for each (system, user) pair."""

    def __init__(self, responder: Callable[[str, str, float, int], dict | None], name: str = "fake"):
        self.responder = responder
        self.name = name
        self.calls = 0

    def chat_json(self, system: str, user: str, *, temperature: float = 0.2, seed: int = 0,
                  cache: bool = True) -> dict | None:
        self.calls += 1
        return self.responder(system, user, temperature, seed)

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        return None


_status_lock = threading.Lock()
_status: dict[str, Any] = {"checked": 0.0, "ok": False, "detail": "not checked"}


def ollama_status(url: str, model: str, force: bool = False) -> dict:
    """Is Ollama reachable and is the model pulled? Cached for 60 seconds."""
    with _status_lock:
        if not force and time.time() - _status["checked"] < 60:
            return dict(_status)
        try:
            r = httpx.get(f"{url.rstrip('/')}/api/tags", timeout=4)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
            wanted = model if ":" in model else f"{model}:latest"
            ok = any(n == model or n == wanted for n in names)
            detail = "ready" if ok else f"model '{model}' not pulled — run: ollama pull {model}"
            _status.update(ok=ok, detail=detail, models=names)
        except (httpx.HTTPError, ValueError) as exc:
            _status.update(ok=False, detail=f"Ollama not reachable at {url} ({exc.__class__.__name__})", models=[])
        _status["checked"] = time.time()
        return dict(_status)


def get_llm(settings) -> OllamaLLM | None:
    if settings.llm_mode == "off":
        return None
    if settings.llm_mode == "auto" and not ollama_status(settings.ollama_url, settings.ollama_model)["ok"]:
        return None
    return OllamaLLM(settings.ollama_url, settings.ollama_model, settings.db_path,
                     embed_model=settings.ollama_embed_model)
