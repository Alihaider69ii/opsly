"""Fetcher resource limits, exercised against a real local HTTP server (real sockets)."""

import gzip
import time
import zlib

import pytest

from app.services.crawler.fetcher import FetchError, FetchLimits
from tests.crawler.sites import (
    FakeSite,
    Resp,
    fetcher_for,
    html,
    loopback_fetcher,
    send,
    serve,
)

HTML_TYPES = {"text/html"}


def get(port: int, path: str = "/", **kwargs):
    fetcher = kwargs.pop("fetcher", None) or loopback_fetcher()
    return fetcher.fetch(f"http://site.test:{port}{path}", **kwargs)


def code_of(port: int, path: str = "/", **kwargs) -> str:
    with pytest.raises(FetchError) as exc:
        get(port, path, **kwargs)
    return exc.value.code


def test_basic_fetch_sends_user_agent_and_host_header_and_reads_body():
    seen = {}

    def index(handler):
        seen["ua"] = handler.headers["User-Agent"]
        seen["host"] = handler.headers["Host"]
        seen["accept_encoding"] = handler.headers["Accept-Encoding"]
        send(handler, 200, b"<html><title>Hi</title><p>hello</p></html>")

    with serve({"/": index}) as port:
        result = get(port)
    assert result.status == 200 and result.content_type == "text/html"
    assert b"hello" in result.body
    assert seen["ua"] == "TestBot/1.0"
    assert seen["host"] == f"site.test:{port}"
    assert "gzip" in seen["accept_encoding"]


def test_non_2xx_is_returned_not_raised_and_body_is_not_read():
    with serve({"/missing": lambda h: send(h, 404, b"x" * 5000)}) as port:
        result = get(port, "/missing")
    assert result.status == 404 and result.body == b""


def test_unsupported_content_type_is_refused_before_reading_the_body():
    def binary(handler):
        send(handler, 200, b"\x00" * 1000, {"Content-Type": "application/zip"})

    with serve({"/f": binary}) as port:
        assert code_of(port, "/f", accept_types=HTML_TYPES) == "unsupported_content"


def test_declared_content_length_over_the_limit_is_refused_immediately():
    big = b"a" * 50_000
    with serve({"/big": lambda h: send(h, 200, big)}) as port:
        assert code_of(port, "/big", max_bytes=10_000) == "too_large"


def test_streamed_body_without_content_length_is_cut_off_at_the_limit():
    def endless(handler):
        send(handler, 200, b"", content_length=False)
        try:
            for _ in range(10_000):  # would be ~160 MB
                handler.wfile.write(b"x" * 16384)
        except OSError:
            pass  # the client hung up, as it should

    with serve({"/endless": endless}) as port:
        started = time.monotonic()
        assert code_of(port, "/endless", max_bytes=100_000) == "too_large"
    assert time.monotonic() - started < 5


def test_truncate_mode_returns_the_first_bytes_instead_of_failing():
    with serve({"/big": lambda h: send(h, 200, b"abcdefghij" * 1000)}) as port:
        result = get(port, "/big", max_bytes=1000, truncate=True)
    assert result.body == (b"abcdefghij" * 1000)[:1000]


def test_gzip_bomb_is_stopped_by_the_decoded_size_limit():
    bomb = gzip.compress(b"\x00" * 50_000_000)  # ~50 KB on the wire, 50 MB decoded
    assert len(bomb) < 100_000

    with serve({"/bomb": lambda h: send(h, 200, bomb, {"Content-Encoding": "gzip"})}) as port:
        assert code_of(port, "/bomb", max_bytes=1_000_000) == "too_large"


def test_gzip_bomb_never_expands_in_memory_beyond_the_limit():
    """Rejecting the response is not enough: decoding must be bounded as it happens, otherwise a
    tiny hostile response still allocates hundreds of megabytes before being refused."""
    import tracemalloc

    bomb = gzip.compress(b"\x00" * 150_000_000)
    with serve({"/bomb": lambda h: send(h, 200, bomb, {"Content-Encoding": "gzip"})}) as port:
        fetcher = loopback_fetcher()
        tracemalloc.start()
        try:
            with pytest.raises(FetchError) as exc:
                fetcher.fetch(f"http://site.test:{port}/bomb", max_bytes=1_000_000)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    assert exc.value.code == "too_large"
    assert peak < 20_000_000, f"decoding allocated {peak / 1e6:.0f} MB for a 1 MB limit"


def test_inflater_output_is_bounded_per_call():
    from app.services.crawler.fetcher import _Inflater

    inflater = _Inflater()
    out = inflater.feed(gzip.compress(b"\x00" * 20_000_000), 1000)
    assert len(out) == 1001  # the limit plus one byte, so the caller can detect overflow


def test_gzip_and_deflate_bodies_are_decoded():
    payload = b"<html><p>compressed hello</p></html>"
    with serve(
        {
            "/gz": lambda h: send(h, 200, gzip.compress(payload), {"Content-Encoding": "gzip"}),
            "/df": lambda h: send(h, 200, zlib.compress(payload), {"Content-Encoding": "deflate"}),
        }
    ) as port:
        assert get(port, "/gz").body == payload
        assert get(port, "/df").body == payload


def test_unsupported_content_encoding_is_rejected():
    with serve({"/br": lambda h: send(h, 200, b"zzz", {"Content-Encoding": "br"})}) as port:
        assert code_of(port, "/br") == "bad_response"


def test_corrupt_gzip_is_a_bad_response():
    with serve({"/x": lambda h: send(h, 200, b"not gzip", {"Content-Encoding": "gzip"})}) as port:
        assert code_of(port, "/x") == "bad_response"


def test_server_that_never_answers_hits_the_read_timeout():
    def silent(handler):
        time.sleep(3)

    limits = FetchLimits(read_timeout=0.4, request_timeout=5)
    with serve({"/slow": silent}) as port:
        started = time.monotonic()
        assert code_of(port, "/slow", fetcher=loopback_fetcher(limits)) == "timeout"
    assert time.monotonic() - started < 2.5


def test_slow_loris_body_is_cut_off_by_the_total_request_deadline():
    """Each byte arrives inside the per-read timeout, so only a wall-clock deadline stops it."""

    def trickle(handler):
        send(handler, 200, b"", {"Content-Length": "100000"}, content_length=False)
        try:
            for _ in range(200):
                handler.wfile.write(b"x")
                handler.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass

    limits = FetchLimits(read_timeout=2, request_timeout=1.0)
    with serve({"/loris": trickle}) as port:
        started = time.monotonic()
        assert code_of(port, "/loris", fetcher=loopback_fetcher(limits)) == "timeout"
    assert time.monotonic() - started < 4


def test_connection_refused_is_reported_as_connect():
    with serve({}) as port:
        pass  # server is closed again: nothing listens on this port
    assert code_of(port) == "connect"


def test_malformed_response_is_a_bad_response():
    def garbage(handler):
        handler.wfile.write(b"this is not http\r\n\r\n")
        handler.close_connection = True

    with serve({"/g": garbage}) as port:
        assert code_of(port, "/g") == "bad_response"


# --- the fake transport covers what sockets can't express easily ---------------------------------


def test_response_headers_charset_is_parsed():
    site = FakeSite({"/": Resp(200, b"x", {"content-type": "text/html; charset=ISO-8859-1"})})
    result = fetcher_for(site).fetch("https://shop.example/")
    assert (result.content_type, result.charset) == ("text/html", "iso-8859-1")


def test_missing_content_type_is_not_html():
    site = FakeSite({"/": Resp(200, b"<p>x</p>", {})})
    with pytest.raises(FetchError) as exc:
        fetcher_for(site).fetch("https://shop.example/", accept_types=HTML_TYPES)
    assert exc.value.code == "unsupported_content"


def test_user_agent_is_sent():
    seen = {}

    def spy(request):
        seen["ua"] = request.headers["user-agent"]
        seen["cookie"] = request.headers.get("cookie")
        seen["authorization"] = request.headers.get("authorization")
        return html("<p>x</p>")

    fetcher_for(FakeSite({"/": spy})).fetch("https://shop.example/")
    assert seen == {"ua": "TestBot/1.0", "cookie": None, "authorization": None}
