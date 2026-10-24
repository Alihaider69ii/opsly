"""The AI adapters over REAL TCP sockets, against a local server that misbehaves on purpose.

The mocked-transport tests prove the translation; these prove the networking: that timeouts fire,
that a refused connection or a half-sent body is a normalized error, that a slow-drip or oversized
or compressed body cannot hold a worker or exhaust memory, that redirects and proxy variables are
ignored, and that keys travel only in headers. No external service is contacted: the server binds
127.0.0.1 on an ephemeral port.
"""

import gzip
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.services.ai.errors import AIErrorCode, EmbeddingError, LLMError
from app.services.ai.provider import AIRequest, ChatMessage
from app.services.ai.providers import _http
from app.services.ai.providers.anthropic_messages import AnthropicMessagesProvider
from app.services.ai.providers.openai_compatible import (
    OpenAICompatibleChatProvider,
    OpenAICompatibleEmbeddingProvider,
)

KEY = "sk-live-SUPERSECRET-0123456789"
REQ = AIRequest(
    messages=[ChatMessage("system", "Be brief."), ChatMessage("user", "Shipping?")], max_tokens=30
)

CHAT_OK = {
    "model": "served",
    "choices": [{"message": {"content": "Two days."}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
}
ANTHROPIC_OK = {
    "model": "claude-served",
    "content": [{"type": "text", "text": "Two days."}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 7, "output_tokens": 3},
}


@pytest.fixture(autouse=True)
def _reuse_one_ssl_context(monkeypatch):
    """Building an httpx client loads the CA bundle (about a second on Windows); share one context.
    These tests talk plain HTTP to 127.0.0.1, so TLS is not involved."""
    import ssl

    import httpx._transports.default as transport

    context = ssl.create_default_context()
    monkeypatch.setattr(transport, "create_ssl_context", lambda *a, **k: context)


class Behaviour:
    """What the server does with the next requests, and what it saw."""

    def __init__(self) -> None:
        self.script: list = []  # callables taking the handler; the last one repeats
        self.seen: list[dict] = []

    def next(self):
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


def reply(status=200, body=None, headers=None, raw: bytes | None = None):
    def action(h: BaseHTTPRequestHandler):
        payload = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(payload)))
        h.end_headers()
        h.wfile.write(payload)

    return action


def stall(seconds: float):
    def action(h):
        time.sleep(seconds)
        reply(200, CHAT_OK)(h)

    return action


def drip(total_chunks: int, gap: float):
    """Valid headers, then the body one byte at a time, slower than any sane client will wait."""

    def action(h):
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        try:
            for _ in range(total_chunks):
                h.wfile.write(b"1\r\n{\r\n")
                h.wfile.flush()
                time.sleep(gap)
            h.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass  # the client gave up, which is the point

    return action


def truncated():
    """Promises 1,000 bytes, sends 10, then hangs up."""

    def action(h):
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", "1000")
        h.end_headers()
        h.wfile.write(b'{"choices"')
        h.wfile.flush()
        h.close_connection = True

    return action


@pytest.fixture()
def server():
    behaviour = Behaviour()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            behaviour.seen.append(
                {"path": self.path, "headers": dict(self.headers.items()), "body": body}
            )
            behaviour.next()(self)

    class Quick(ThreadingHTTPServer):
        daemon_threads = True  # do not wait for handlers that are deliberately stalling
        block_on_close = False

    httpd = Quick(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    behaviour.url = f"http://127.0.0.1:{httpd.server_address[1]}"  # type: ignore[attr-defined]
    yield behaviour
    httpd.shutdown()
    httpd.server_close()


def chat(server, **kwargs) -> OpenAICompatibleChatProvider:
    kwargs.setdefault("sleep", lambda seconds: None)
    return OpenAICompatibleChatProvider(
        api_key=KEY, model="m", base_url=server.url + "/v1", provider_id="gateway", **kwargs
    )


# --- the happy path really works over a socket -------------------------------------------------------------


def test_a_real_round_trip_sends_the_key_only_in_a_header_and_normalizes_the_reply(server):
    server.script = [reply(200, CHAT_OK, {"x-request-id": "req-abc"})]
    response = chat(server).generate(REQ)
    assert response.text == "Two days." and response.request_id == "req-abc"
    assert (response.usage.prompt_tokens, response.usage.total_tokens) == (7, 10)  # type: ignore[union-attr]
    seen = server.seen[0]
    assert seen["path"] == "/v1/chat/completions"
    assert seen["headers"]["Authorization"] == f"Bearer {KEY}"
    assert KEY.encode() not in seen["body"] and b"Shipping?" in seen["body"]


def test_the_anthropic_adapter_over_a_real_socket(server):
    server.script = [reply(200, ANTHROPIC_OK, {"request-id": "req_011"})]
    provider = AnthropicMessagesProvider(
        api_key=KEY, model="m", base_url=server.url, sleep=lambda s: None
    )
    response = provider.generate(REQ)
    assert response.text == "Two days." and response.request_id == "req_011"
    seen = server.seen[0]
    assert seen["path"] == "/v1/messages" and seen["headers"]["x-api-key"] == KEY
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in seen["headers"]
    assert json.loads(seen["body"])["system"] == "Be brief."


# --- timeouts and unreachable servers -------------------------------------------------------------------------


def test_a_server_that_never_answers_is_a_timeout_not_a_hang(server):
    server.script = [stall(2.0)]
    started = time.monotonic()
    with pytest.raises(LLMError) as exc:
        chat(server, timeout=0.3, max_attempts=2).generate(REQ)
    assert exc.value.code == AIErrorCode.TIMEOUT and exc.value.retryable
    assert time.monotonic() - started < 1.9  # two bounded attempts, not the server's 2 seconds
    assert len(server.seen) == 2


def test_a_refused_connection_is_unavailable(server):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here now
    provider = OpenAICompatibleChatProvider(
        api_key=KEY,
        model="m",
        base_url=f"http://127.0.0.1:{port}/v1",
        timeout=0.5,
        max_attempts=2,
        sleep=lambda s: None,
    )
    with pytest.raises(LLMError) as exc:
        provider.generate(REQ)
    # Linux refuses at once (unavailable); Windows lets the connect attempt time out. Both are the
    # same fact to the caller: nobody is there, and trying again later is reasonable.
    assert exc.value.code in (AIErrorCode.UNAVAILABLE, AIErrorCode.TIMEOUT) and exc.value.retryable
    assert KEY not in str(exc.value)


def test_a_connection_dropped_mid_body_is_unavailable_and_retried(server):
    server.script = [truncated(), reply(200, CHAT_OK)]
    assert chat(server, max_attempts=2).generate(REQ).text == "Two days."
    assert len(server.seen) == 2


def test_a_body_dripped_one_byte_at_a_time_cannot_hold_the_caller_for_ever(server):
    """httpx's read timeout restarts with every byte; the wall-clock deadline does not."""
    server.script = [drip(total_chunks=60, gap=0.12)]  # would take ~7 seconds
    started = time.monotonic()
    with pytest.raises(LLMError) as exc:
        chat(server, timeout=0.25, max_attempts=1).generate(REQ)
    assert exc.value.code == AIErrorCode.TIMEOUT
    assert time.monotonic() - started < 2.5


# --- statuses over the wire --------------------------------------------------------------------------------------


def test_a_rate_limit_is_retried_honouring_retry_after_then_succeeds(server):
    sleeps: list[float] = []
    server.script = [
        reply(429, {"error": {"code": "rate_limit"}}, {"Retry-After": "3"}),
        reply(200, CHAT_OK),
    ]
    assert chat(server, sleep=sleeps.append).generate(REQ).text == "Two days."
    assert sleeps == [3.0] and len(server.seen) == 2


def test_a_provider_5xx_is_retried_a_bounded_number_of_times(server):
    server.script = [reply(503, {"error": "overloaded"})]
    with pytest.raises(LLMError) as exc:
        chat(server, max_attempts=3).generate(REQ)
    assert exc.value.code == AIErrorCode.UNAVAILABLE and len(server.seen) == 3


def test_an_invalid_key_is_not_retried_and_the_error_has_no_key_or_vendor_text(server):
    server.script = [reply(401, {"error": {"message": f"Incorrect API key provided: {KEY}"}})]
    with pytest.raises(LLMError) as exc:
        chat(server, max_attempts=3).generate(REQ)
    assert exc.value.code == AIErrorCode.AUTH and len(server.seen) == 1
    assert KEY not in str(exc.value) and "Incorrect" not in str(exc.value)


# --- malformed, empty and oversized replies -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"<html>502 Bad Gateway</html>",
        b"",
        b'{"choices": []}',
        b'{"choices": [{"message": {"content": ""}}]}',
        b'{"choices": [{"message": {"content": null}}]}',
        b"[1, 2, 3]",
        b'{"choices": [{"message":',
    ],
)
def test_garbage_empty_and_blank_replies_are_provider_errors_never_answers(server, raw):
    server.script = [reply(200, raw=raw)]
    with pytest.raises(LLMError) as exc:
        chat(server, max_attempts=2).generate(REQ)
    assert exc.value.code == AIErrorCode.PROVIDER_ERROR
    assert len(server.seen) == 1  # a reply that cannot be understood is not retried


def test_an_oversized_reply_is_cut_off_and_not_retried(server, monkeypatch):
    monkeypatch.setattr(_http, "MAX_RESPONSE_BYTES", 10_000)
    server.script = [reply(200, raw=b'{"x": "' + b"a" * 200_000 + b'"}')]
    with pytest.raises(LLMError) as exc:
        chat(server, max_attempts=3).generate(REQ)
    assert exc.value.code == AIErrorCode.PROVIDER_ERROR and len(server.seen) == 1


def test_a_compression_bomb_is_counted_after_decompression(server, monkeypatch):
    monkeypatch.setattr(_http, "MAX_RESPONSE_BYTES", 10_000)
    bomb = gzip.compress(b'{"x": "' + b"a" * 1_000_000 + b'"}')  # ~1 KB on the wire, 1 MB decoded
    assert len(bomb) < 5_000
    server.script = [reply(200, raw=bomb, headers={"Content-Encoding": "gzip"})]
    with pytest.raises(LLMError) as exc:
        chat(server).generate(REQ)
    assert exc.value.code == AIErrorCode.PROVIDER_ERROR


def _padded_valid_reply(padding: int) -> bytes:
    """A reply that is a perfectly good chat answer, plus junk: only its SIZE can make it wrong."""
    return json.dumps({**CHAT_OK, "padding": "a" * padding}).encode()


def test_a_valid_reply_is_refused_only_because_it_is_larger_than_the_cap(server, monkeypatch):
    monkeypatch.setattr(_http, "MAX_RESPONSE_BYTES", 10_000)
    server.script = [reply(200, raw=_padded_valid_reply(200_000))]
    with pytest.raises(LLMError) as exc:
        chat(server, max_attempts=3).generate(REQ)
    assert exc.value.code == AIErrorCode.PROVIDER_ERROR and len(server.seen) == 1

    monkeypatch.setattr(_http, "MAX_RESPONSE_BYTES", 1_000_000)  # same bytes, a larger allowance
    server.script = [reply(200, raw=_padded_valid_reply(200_000))]
    assert chat(server).generate(REQ).text == "Two days."


def test_a_valid_compressed_reply_that_expands_past_the_cap_is_refused(server, monkeypatch):
    monkeypatch.setattr(_http, "MAX_RESPONSE_BYTES", 10_000)
    packed = gzip.compress(_padded_valid_reply(1_000_000))
    assert len(packed) < 5_000  # tiny on the wire, a megabyte once decoded
    server.script = [reply(200, raw=packed, headers={"Content-Encoding": "gzip"})]
    with pytest.raises(LLMError) as exc:
        chat(server).generate(REQ)
    assert exc.value.code == AIErrorCode.PROVIDER_ERROR


def test_an_enormous_error_body_does_not_change_how_the_error_is_classified(server, monkeypatch):
    monkeypatch.setattr(_http, "MAX_ERROR_BODY_BYTES", 1_000)
    server.script = [reply(401, raw=b'{"error": {"message": "' + b"x" * 500_000 + b'"}}')]
    with pytest.raises(LLMError) as exc:
        chat(server).generate(REQ)
    assert exc.value.code == AIErrorCode.AUTH  # status alone classifies it; the body is cut short


def test_a_compressed_but_ordinary_reply_still_works(server):
    server.script = [
        reply(
            200,
            raw=gzip.compress(json.dumps(CHAT_OK).encode()),
            headers={"Content-Encoding": "gzip"},
        )
    ]
    assert chat(server).generate(REQ).text == "Two days."


# --- the client refuses what it should refuse ---------------------------------------------------------------------------


def test_a_redirect_is_never_followed(server):
    """A hostile or hijacked gateway must not be able to bounce the request (and the key) elsewhere."""
    other = Behaviour()
    server.script = [reply(307, {}, {"Location": "http://127.0.0.1:9/steal"})]
    with pytest.raises(LLMError):
        chat(server, max_attempts=1).generate(REQ)
    assert len(server.seen) == 1 and other.seen == []


def test_proxy_environment_variables_are_ignored(server, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # a dead proxy: honouring it would fail
    server.script = [reply(200, CHAT_OK)]
    assert chat(server).generate(REQ).text == "Two days."


# --- embeddings over a socket -----------------------------------------------------------------------------------------------


def embeddings(server, dims, **kwargs) -> OpenAICompatibleEmbeddingProvider:
    return OpenAICompatibleEmbeddingProvider(
        api_key=KEY,
        model="m",
        dimensions=dims,
        base_url=server.url + "/v1",
        provider_id="gateway",
        sleep=lambda s: None,
        **kwargs,
    )


def vectors(n, width):
    return {"data": [{"index": i, "embedding": [0.1] * width} for i in range(n)]}


def test_embeddings_round_trip_and_batching_over_a_real_socket(server):
    server.script = [reply(200, vectors(2, 8)), reply(200, vectors(1, 8))]
    out = embeddings(server, 8, batch_size=2).embed(["a", "b", "c"])
    assert len(out) == 3 and all(len(v) == 8 for v in out)
    assert [len(json.loads(s["body"])["input"]) for s in server.seen] == [2, 1]


def test_a_model_of_the_wrong_width_is_refused_not_stored(server):
    """Vendors that serve 1024- or 3072-wide models behind an OpenAI-style endpoint must fail closed:
    the knowledge_chunks column is 1536 wide and a mismatch must never reach the database."""
    server.script = [reply(200, vectors(1, 1024))]
    with pytest.raises(EmbeddingError) as exc:
        embeddings(server, 1536).embed(["x"])
    assert exc.value.code == AIErrorCode.MISCONFIGURED
    assert "1024" in str(exc.value) and "1536" in str(exc.value) and len(server.seen) == 1


def test_fewer_vectors_than_inputs_is_a_provider_error(server):
    server.script = [reply(200, vectors(1, 8))]
    with pytest.raises(EmbeddingError) as exc:
        embeddings(server, 8).embed(["a", "b"])
    assert exc.value.code == AIErrorCode.PROVIDER_ERROR
