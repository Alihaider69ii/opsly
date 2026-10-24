"""Test doubles for websites: an in-memory fake (httpx.MockTransport) and a real local server."""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from app.services.crawler.fetcher import FetchLimits, SafeFetcher

PUBLIC_IP = "93.184.216.34"  # a genuinely public address; nothing is ever connected to it


@dataclass
class Resp:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


def html(body: str, status: int = 200) -> Resp:
    return Resp(status, body.encode(), {"content-type": "text/html; charset=utf-8"})


def page(title: str, body: str, links: tuple[str, ...] = ()) -> Resp:
    anchors = "".join(f'<a href="{href}">link</a>' for href in links)
    return html(
        f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1>"
        f"<p>{body}</p>{anchors}</main></body></html>"
    )


def redirect(location: str, status: int = 301) -> Resp:
    return Resp(status, b"", {"location": location})


def text(body: str, status: int = 200) -> Resp:
    return Resp(status, body.encode(), {"content-type": "text/plain"})


Route = Resp | Callable[[httpx.Request], Resp]


class FakeSite:
    """Routes are keyed "/path?query" (any host) or "host/path?query" (one host).

    ``requests`` records (host header, path+query, connected address) for every request, so tests
    can assert exactly what the crawler did and did not touch.
    """

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, str]] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers.get("host", "")
        bare_host = host.split(":")[0]
        key = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        self.requests.append((host, key, request.url.host))
        route = self.routes.get(f"{bare_host}{key}") or self.routes.get(key)
        if route is None:
            return httpx.Response(
                404, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(b"not found")
            )
        resp = route(request) if callable(route) else route
        # A real transport hands back an unconsumed stream; ByteStream mimics that so the
        # fetcher's streaming read path is what gets exercised.
        return httpx.Response(resp.status, headers=resp.headers, stream=httpx.ByteStream(resp.body))

    def paths(self) -> list[str]:
        return [path for _, path, _ in self.requests]


def public_resolver(host: str, port: int) -> list[str]:
    return [PUBLIC_IP]


def fetcher_for(
    site: FakeSite,
    *,
    resolver: Callable[[str, int], list[str]] = public_resolver,
    limits: FetchLimits | None = None,
) -> SafeFetcher:
    return SafeFetcher(
        user_agent="TestBot/1.0",
        limits=limits or FetchLimits(),
        resolver=resolver,
        transport=site.transport,
    )


# --- real sockets -------------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        handler = self.server.routes.get(self.path)  # type: ignore[attr-defined]
        if handler is None:
            send(self, 404, b"nope")
        else:
            handler(self)

    def log_message(self, *args: object) -> None:  # silence test output
        pass


def send(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes,
    headers: dict[str, str] | None = None,
    *,
    content_length: bool = True,
) -> None:
    handler.send_response(status)
    extra = headers or {}
    if not any(name.lower() == "content-type" for name in extra):
        handler.send_header("Content-Type", "text/html")
    for name, value in extra.items():
        handler.send_header(name, value)
    if content_length:
        handler.send_header("Content-Length", str(len(body)))
    else:
        handler.send_header("Connection", "close")
        handler.close_connection = True
    handler.end_headers()
    handler.wfile.write(body)


@contextmanager
def serve(routes: dict[str, Callable[[BaseHTTPRequestHandler], None]]) -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.routes = routes  # type: ignore[attr-defined]
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def loopback_fetcher(limits: FetchLimits | None = None) -> SafeFetcher:
    """Fetcher that may reach 127.0.0.1 on any port. Test-only: production code builds its fetcher
    with build_fetcher(), which always uses the strict public-address policy."""
    return SafeFetcher(
        user_agent="TestBot/1.0",
        limits=limits or FetchLimits(),
        resolver=lambda host, port: ["127.0.0.1"],
        ip_validator=lambda ip: True,
        allowed_ports=None,
    )
