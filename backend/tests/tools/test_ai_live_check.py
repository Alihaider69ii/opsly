"""The AI live-check tool itself, run against a local server that behaves like a real vendor.

A live check that has never been run is a liability: this proves the tool's logic (what it asserts,
what it prints, that it never prints the key) so that, when it is pointed at a real provider, a
failure means the provider is wrong and not the tool.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from app.services.ai.provider import ChatMessage
from app.services.ai.providers.fake import FakeEmbeddingProvider, FakeLLMProvider
from tools import ai_live_check

GOOD_KEY = "sk-live-GOOD-KEY-0123456789"


@pytest.fixture(autouse=True)
def _reuse_one_ssl_context(monkeypatch):
    import ssl

    import httpx._transports.default as transport

    context = ssl.create_default_context()
    monkeypatch.setattr(transport, "create_ssl_context", lambda *a, **k: context)


@pytest.fixture(autouse=True)
def _fresh_tool_state():
    ai_live_check.FAILURES.clear()


class Vendor(BaseHTTPRequestHandler):
    """A tiny OpenAI-compatible vendor: real auth, real model check, real (hashed) embeddings."""

    protocol_version = "HTTP/1.1"
    embed = FakeEmbeddingProvider()
    chat = FakeLLMProvider()

    def log_message(self, *args):
        pass

    def _send(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("x-request-id", "req-live-1")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != f"Bearer {GOOD_KEY}":
            return self._send(
                401, {"error": {"message": "Incorrect API key", "code": "invalid_api_key"}}
            )
        if "no-such-model" in str(body.get("model")):
            return self._send(
                404, {"error": {"message": "The model does not exist", "code": "model_not_found"}}
            )
        if self.path.endswith("/chat/completions"):
            time.sleep(0.05)  # a remote model never answers within the tool's 1 ms timeout probe
            messages = [ChatMessage(m["role"], m["content"]) for m in body["messages"]]
            text = self.chat._answer(messages)
            return self._send(
                200,
                {
                    "model": body["model"],
                    "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 6},
                },
            )
        vectors = self.embed.embed(body["input"])
        return self._send(
            200, {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
        )


@pytest.fixture()
def vendor():
    class Quick(ThreadingHTTPServer):
        daemon_threads = True
        block_on_close = False

    httpd = Quick(("127.0.0.1", 0), Vendor)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()
    httpd.server_close()


def configured(base_url, **overrides):
    return get_settings().model_copy(
        update={
            "llm_provider": "openai",  # a provider that requires a key, pointed at the local vendor
            "llm_base_url": base_url,
            "llm_model": "vendor-chat",
            "llm_api_key": SecretStr(GOOD_KEY),
            "embedding_provider": "openai",
            "embedding_base_url": base_url,
            "embedding_model": "vendor-embed",
            "embedding_api_key": SecretStr(GOOD_KEY),
            "openai_api_key": None,
            **overrides,
        }
    )


def test_a_healthy_provider_passes_every_check_and_the_output_never_shows_a_key(vendor, capsys):
    settings = configured(vendor)
    ai_live_check.check_chat(settings)
    ai_live_check.check_embedding_and_rag(settings)
    out = capsys.readouterr().out
    assert ai_live_check.FAILURES == [], out
    for expected in (
        "a real chat request returned text",
        "an invalid API key is reported as auth   [observed code=auth]",
        "a request that cannot finish in time is reported as timeout",
        "a model that does not exist is a normalized error   [observed code=model_unavailable]",
        "vector width matches the database column",
        "a question the documents answer is grounded",
        "an unrelated question gets the fallback WITHOUT calling the model",
    ):
        assert expected in out, expected
    assert "req-live-1" in out  # the vendor request id is reported for support tickets
    assert (
        GOOD_KEY not in out and "bizflow-invalid-key" not in out and "Incorrect API key" not in out
    )


def test_the_tool_fails_loudly_when_the_real_request_is_rejected(vendor, capsys):
    settings = configured(vendor, llm_api_key=SecretStr("sk-wrong"))
    ai_live_check.check_chat(settings)
    out = capsys.readouterr().out
    assert ai_live_check.FAILURES == ["a real chat request"]
    assert "code=auth" in out and "sk-wrong" not in out


def test_the_tool_reports_a_misclassified_failure_instead_of_passing_it(
    vendor, capsys, monkeypatch
):
    """If an adapter ever let a raw vendor error escape, the tool must say so."""
    from app.services.ai import factory

    settings = configured(vendor)
    real = factory.build_llm_provider
    calls = {"n": 0}

    def leaky(s):
        provider = real(s)
        calls["n"] += 1
        if calls["n"] >= 2:  # the invalid-key probe gets a provider that raises a raw error
            provider.generate = lambda request: (_ for _ in ()).throw(
                RuntimeError("raw vendor boom")
            )
        return provider

    monkeypatch.setattr(factory, "build_llm_provider", leaky)
    ai_live_check.check_chat(settings)
    out = capsys.readouterr().out
    assert "NOT NORMALIZED: RuntimeError" in out
    assert "an invalid API key is reported as auth" in ai_live_check.FAILURES
    assert "raw vendor boom" not in out  # the message of an unexpected error is not echoed


def test_an_unconfigured_provider_is_a_failed_check_not_a_crash(capsys):
    settings = get_settings().model_copy(
        update={"llm_provider": "openai", "llm_api_key": None, "openai_api_key": None}
    )
    ai_live_check.check_chat(settings)
    out = capsys.readouterr().out
    assert ai_live_check.FAILURES == ["build the provider from configuration"]
    assert "code=unconfigured" in out


def test_the_fake_provider_is_accepted_but_clearly_marked_as_not_a_real_check(capsys):
    settings = get_settings().model_copy(
        update={"llm_provider": "fake", "embedding_provider": "fake"}
    )
    ai_live_check.check_chat(settings)
    ai_live_check.check_embedding_and_rag(settings)
    out = capsys.readouterr().out
    assert ai_live_check.FAILURES == []
    assert "skipped: the fake provider has none" in out and "not semantic" in out
