"""``FakeShopify`` served over real HTTP, for tests that need a separate process to talk to Shopify.

The in-process fake (``fake_shopify.py``) answers through an ``httpx.MockTransport``, which a worker
running in another process cannot use. This wraps the very same object in a threaded HTTP server, so
the real worker, the real Shopify client and real sockets are exercised against identical behaviour.

* **Gates** hold a matching request open so a test can stop the other process at an exact moment:
  ``before`` (Shopify has not yet acted) or ``after`` (Shopify has acted, e.g. rotated the refresh
  token, but the client has not yet received the answer).
* **Control endpoints** (``/__control/*``) and ``/admin/oauth/authorize`` let a browser-driven run
  seed data and approve the app. They exist only when the server is started for that purpose.

Run standalone for the browser end-to-end:  ``python -m tests.shopify.http_server --port 9400``
"""

import argparse
import json
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from app.services.integrations.shopify.validation import sign_oauth_query
from tests.shopify.fake_shopify import (
    FakeShopify,
    make_customer,
    make_line_item,
    make_order,
    make_product,
    make_variant,
)

GATE_TIMEOUT_SECONDS = 120.0


class _QuietServer(ThreadingHTTPServer):
    """A client that was killed mid-request is an expected event here, not an error to print."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)


@dataclass(frozen=True)
class Seen:
    """What a request is, in the terms a test cares about."""

    method: str
    path: str
    operation: str  # GraphQL operation name, or "token:<grant_type>"
    variables: dict[str, Any] = field(default_factory=dict)


@dataclass
class Gate:
    predicate: Callable[[Seen], bool]
    after: bool = False
    skip: int = 0  # let this many matching requests through before holding one
    hits: int = 0
    reached: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def wait_reached(self, timeout: float = 60.0) -> bool:
        return self.reached.wait(timeout)

    def open(self) -> None:
        self.release.set()


def operation_is(name: str) -> Callable[[Seen], bool]:
    return lambda seen: seen.operation == name


def classify(path: str, body: bytes) -> tuple[str, dict[str, Any]]:
    if path.endswith("/oauth/access_token"):
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        return f"token:{form.get('grant_type', 'authorization_code')}", {}
    if "graphql" in path:
        try:
            payload = json.loads(body)
            match = re.search(r"(?:query|mutation)\s+(\w+)", payload.get("query", ""))
            return (match.group(1) if match else ""), payload.get("variables") or {}
        except (ValueError, AttributeError):
            return "", {}
    return "", {}


class FakeShopifyHttpServer:
    def __init__(
        self,
        fake: FakeShopify | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        interactive: bool = False,
    ) -> None:
        self.fake = fake or FakeShopify()
        self.lock = threading.Lock()  # the fake is not thread-safe; serialize access to it
        self.gates: list[Gate] = []
        self.seen: list[Seen] = []
        self.interactive = interactive  # serve /admin/oauth/authorize and /__control/*
        self.authorize_mode = "approve"
        self.graphql_delay = 0.0
        self.authorize_calls = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                outer._get(self)

            def do_POST(self) -> None:
                outer._post(self)

        self._server = _QuietServer((host, port), Handler)
        self._server.daemon_threads = True
        self._server.block_on_close = False
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # -- lifecycle -----------------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FakeShopifyHttpServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        for gate in self.gates:
            gate.open()  # never leave a handler thread waiting on a test that has finished
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "FakeShopifyHttpServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def gate(
        self, predicate: Callable[[Seen], bool], *, after: bool = False, skip: int = 0
    ) -> Gate:
        gate = Gate(predicate, after=after, skip=skip)
        self.gates.append(gate)
        return gate

    def count(self, operation: str) -> int:
        return sum(1 for seen in self.seen if seen.operation == operation)

    # -- request handling ------------------------------------------------------------------------

    @staticmethod
    def _send(
        h: BaseHTTPRequestHandler,
        status: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        try:
            h.send_response(status)
            for key, value in (headers or {}).items():
                h.send_header(key, value)
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
        except (BrokenPipeError, ConnectionError, OSError):
            pass  # the client was killed while we were answering: that is the point of some tests

    def _json(self, h: BaseHTTPRequestHandler, status: int, obj: Any) -> None:
        self._send(h, status, json.dumps(obj).encode(), {"Content-Type": "application/json"})

    @staticmethod
    def _body(h: BaseHTTPRequestHandler) -> bytes:
        length = int(h.headers.get("Content-Length") or 0)
        return h.rfile.read(length) if length else b""

    def _hold(self, seen: Seen, *, after: bool) -> None:
        for gate in list(self.gates):
            if gate.after is not after or not gate.predicate(seen):
                continue
            gate.hits += 1
            if gate.hits > gate.skip:
                gate.reached.set()
                gate.release.wait(GATE_TIMEOUT_SECONDS)

    def _get(self, h: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(h.path)
        if self.interactive and parts.path == "/admin/oauth/authorize":
            return self._authorize(h, parse_qs(parts.query))
        if self.interactive and parts.path == "/__control/state":
            with self.lock:
                return self._json(
                    h,
                    200,
                    {
                        "subscriptions": len(self.fake.subscriptions),
                        "token_requests": len(self.fake.token_requests),
                        "operations": len(self.fake.requests),
                        "valid_access": len(self.fake.valid_access),
                        "mode": self.authorize_mode,
                        "authorize_calls": self.authorize_calls,
                        "tokens": sorted(self.fake.valid_access | self.fake.valid_refresh),
                    },
                )
        self._json(h, 404, {"errors": "not found"})

    def _post(self, h: BaseHTTPRequestHandler) -> None:
        parts = urlsplit(h.path)
        body = self._body(h)
        if self.interactive and parts.path.startswith("/__control/"):
            return self._control(
                h, parts.path.removeprefix("/__control/"), json.loads(body or b"{}")
            )
        operation, variables = classify(parts.path, body)
        seen = Seen("POST", parts.path, operation, variables)
        self.seen.append(seen)
        if self.graphql_delay and "graphql" in parts.path:
            time.sleep(self.graphql_delay)
        self._hold(seen, after=False)
        request = httpx.Request(
            "POST",
            f"http://{h.headers.get('Host')}{h.path}",
            headers=dict(h.headers.items()),
            content=body,
        )
        with self.lock:
            response = self.fake.handler(request)
        self._hold(seen, after=True)
        self._send(
            h,
            response.status_code,
            response.content,
            {
                "Content-Type": response.headers.get("content-type", "application/json"),
                **{k: v for k, v in response.headers.items() if k.lower() == "retry-after"},
            },
        )

    # -- browser-run extras ----------------------------------------------------------------------

    def _authorize(self, h: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        with self.lock:
            self.authorize_calls += 1
        redirect_uri = query["redirect_uri"][0]
        if query.get("client_id", [""])[0] != self.fake.client_id:
            return self._send(h, 400, b"bad client")
        if self.authorize_mode == "decline":
            target = (
                redirect_uri + "?" + urlencode({"error": "access_denied", "shop": self.fake.shop})
            )
            return self._send(h, 302, b"", {"Location": target})
        self.fake.valid_codes.add("good-code")
        callback = {
            "code": "good-code",
            "host": "ZTJlLXNob3A",
            "shop": self.fake.shop,
            "state": query["state"][0],
            "timestamp": str(int(time.time())),
        }
        secret = "forged-secret" if self.authorize_mode == "forge" else self.fake.client_secret
        callback["hmac"] = sign_oauth_query(callback, secret)
        self._send(h, 302, b"", {"Location": redirect_uri + "?" + urlencode(callback)})

    def seed(self, products: int, customers: int, orders: int) -> None:
        self.fake.products = [
            make_product(
                n,
                title=f"Soap {n}",
                variants=[
                    make_variant(n * 10 + 1, price="8.00"),
                    make_variant(n * 10 + 2, price="9.50"),
                ],
            )
            for n in range(1, products + 1)
        ]
        self.fake.customers = [
            make_customer(n, email=f"shopper{n}@private-example.test", phone=f"+1555000{n:04d}")
            for n in range(1, customers + 1)
        ]
        self.fake.orders = [
            make_order(
                n,
                customer=(n % customers) + 1 if customers else None,
                items=[make_line_item(n * 100 + 1, product=(n % products) + 1, variant=None)]
                if products
                else [],
            )
            for n in range(1, orders + 1)
        ]

    def _control(self, h: BaseHTTPRequestHandler, action: str, payload: dict[str, Any]) -> None:
        with self.lock:
            if action == "seed":
                self.seed(payload["products"], payload["customers"], payload["orders"])
            elif action == "mode":
                self.authorize_mode = payload["mode"]
            elif action == "delay":
                self.graphql_delay = float(payload["seconds"])
            elif action == "rename_product":
                for product in self.fake.products:
                    if product["id"].endswith(f"/{payload['id']}"):
                        product["title"] = payload["title"]
                        product["updatedAt"] = "2031-01-01T00:00:00Z"
            elif action == "revoke":
                self.fake.revoke_all_tokens()
            else:
                return self._json(h, 404, {"error": "unknown action"})
        self._json(h, 200, {"ok": True})


def main() -> None:
    parser = argparse.ArgumentParser(description="FakeShopify over HTTP for the browser end-to-end")
    parser.add_argument("--port", type=int, default=9400)
    parser.add_argument("--shop", default="e2e-shop.myshopify.com")
    parser.add_argument("--client-id", default="e2e-client-id")
    parser.add_argument("--client-secret", default="e2e-client-secret")
    parser.add_argument("--products", type=int, default=40)
    parser.add_argument("--customers", type=int, default=25)
    parser.add_argument("--orders", type=int, default=30)
    args = parser.parse_args()
    fake = FakeShopify(shop=args.shop, client_id=args.client_id, client_secret=args.client_secret)
    fake.shop_name = "E2E Soap Shop"
    server = FakeShopifyHttpServer(fake, port=args.port, interactive=True)
    server.seed(args.products, args.customers, args.orders)
    server.start()
    print(f"fake shopify on {server.base_url} shop={args.shop}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
