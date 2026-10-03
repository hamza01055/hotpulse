"""OllamaLLM against a tiny fake Ollama HTTP server (same API shape as the real one)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hotpulse.llm import OllamaLLM, ollama_status


class FakeOllama(BaseHTTPRequestHandler):
    calls = []
    reply = '{"score": 77, "reason": "ok"}'

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/api/tags":
            self._send({"models": [{"name": "qwen2.5:7b"}]})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.calls.append(body)
        if self.path == "/api/chat":
            self._send({"message": {"role": "assistant", "content": FakeOllama.reply}, "done": True})
        elif self.path == "/api/embed":
            self._send({"embeddings": [[1.0, 0.0] for _ in body["input"]]})

    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    FakeOllama.calls = []
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_chat_json_and_cache(server, settings):
    llm = OllamaLLM(server, "qwen2.5:7b", settings.db_path, embed_model="nomic-embed-text")
    assert llm.chat_json("sys", "user") == {"score": 77, "reason": "ok"}
    sent = FakeOllama.calls[-1]
    assert sent["format"] == "json" and sent["stream"] is False and sent["messages"][0]["role"] == "system"
    assert llm.chat_json("sys", "user") == {"score": 77, "reason": "ok"}
    assert len(FakeOllama.calls) == 1, "second identical call is served from cache"
    assert llm.embed(["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]]


def test_non_json_reply_returns_none(server, settings):
    FakeOllama.reply = "I cannot do that"
    try:
        llm = OllamaLLM(server, "qwen2.5:7b", settings.db_path)
        assert llm.chat_json("s", "u", cache=False) is None
        assert llm.failures == 1
    finally:
        FakeOllama.reply = '{"score": 77, "reason": "ok"}'


def test_status(server):
    assert ollama_status(server, "qwen2.5:7b", force=True)["ok"] is True
    st = ollama_status(server, "llama3.1:8b", force=True)
    assert st["ok"] is False and "ollama pull llama3.1:8b" in st["detail"]
    assert ollama_status("http://127.0.0.1:9", "x", force=True)["ok"] is False
