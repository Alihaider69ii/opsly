"""What every client sees when things go wrong, and what no client can force the server to do."""

import io
import logging

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.core.logging import configure_logging
from app.main import app

SECRET_DSN = "postgresql://app:SuperSecretPassword@10.1.2.3/bizflow"
PLAIN_REQUEST = {"email": "a@example.com", "password": "correct-horse-battery"}


@pytest.fixture()
def boom():
    """Temporary routes that fail in the ways production code fails, on the real application."""
    from fastapi import APIRouter

    router = APIRouter(prefix="/__t")

    @router.get("/ok")
    def ok():
        logging.getLogger("app.test").info("handled the request")
        return {"ok": True}

    @router.get("/db")
    def database():
        raise OperationalError(
            f"SELECT 1 /* {SECRET_DSN} */", {"password": "SuperSecretPassword"}, Exception("x")
        )

    @router.get("/pool")
    def pool():
        raise PoolTimeoutError("QueuePool limit of size 5 overflow 10 reached")

    @router.get("/redis")
    def redis_down():
        raise redis.ConnectionError(f"Error connecting to {SECRET_DSN}")

    @router.get("/bug")
    def bug():
        raise RuntimeError(f"internal detail {SECRET_DSN} must not be shown")

    before = list(app.router.routes)
    app.include_router(router)
    yield TestClient(app, raise_server_exceptions=False)
    app.router.routes[:] = before  # (included routers are wrapped objects: restore, don't filter)
    app.openapi_schema = None


# --- request ids --------------------------------------------------------------------------------------------------


def test_every_response_carries_a_request_id(boom):
    first, second = boom.get("/__t/ok"), boom.get("/__t/ok")
    assert len(first.headers["x-request-id"]) == 32
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_a_sane_inbound_request_id_is_kept_so_a_proxys_id_can_be_followed(boom):
    r = boom.get("/__t/ok", headers={"X-Request-ID": "trace-abc.123_XYZ"})
    assert r.headers["x-request-id"] == "trace-abc.123_XYZ"


@pytest.mark.parametrize(
    "hostile", ["short", "x" * 200, "has space in it!", "a;b=c&d<script>", "line\tbreak-0000"]
)
def test_a_hostile_inbound_request_id_is_replaced_never_echoed(boom, hostile):
    r = boom.get("/__t/ok", headers={"X-Request-ID": hostile})
    assert r.headers["x-request-id"] != hostile and len(r.headers["x-request-id"]) == 32


def test_the_id_is_on_errors_too(boom):
    assert boom.get("/nothing-here").headers["x-request-id"]
    assert boom.post("/api/auth/login", json={}).headers["x-request-id"]  # a 422


def test_log_lines_written_while_handling_a_request_carry_its_id(boom):
    stream = io.StringIO()
    configure_logging("INFO", stream)
    r = boom.get("/__t/ok", headers={"X-Request-ID": "trace-0001-abcdef"})
    line = next(ln for ln in stream.getvalue().splitlines() if "handled the request" in ln)
    assert line.endswith("request_id=trace-0001-abcdef") and r.status_code == 200


def test_log_lines_outside_a_request_have_no_id_suffix():
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("app.test").info("a background line")
    assert "request_id" not in stream.getvalue()


# --- failures are predictable and say nothing about the cause ------------------------------------------------------------


@pytest.mark.parametrize("path", ["/__t/db", "/__t/pool", "/__t/redis"])
def test_a_dependency_outage_is_a_503_with_retry_after_not_a_500(boom, path):
    r = boom.get(path, headers={"X-Request-ID": "trace-0002-abcdef"})
    assert r.status_code == 503 and r.headers["retry-after"] == "5"
    assert r.json() == {
        "detail": "Service temporarily unavailable. Please retry.",
        "request_id": "trace-0002-abcdef",
    }
    assert r.headers["x-request-id"] == "trace-0002-abcdef"
    for leaked in ("SuperSecret", "10.1.2.3", "postgresql", "SELECT", "QueuePool"):
        assert leaked not in r.text


def test_an_unexpected_error_is_a_generic_500_with_the_id_to_find_it_in_the_logs(boom):
    r = boom.get("/__t/bug", headers={"X-Request-ID": "trace-0003-abcdef"})
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal server error", "request_id": "trace-0003-abcdef"}
    assert r.headers["x-request-id"] == "trace-0003-abcdef"
    assert (
        "SuperSecret" not in r.text and "RuntimeError" not in r.text and "Traceback" not in r.text
    )


# --- request bodies ----------------------------------------------------------------------------------------------------


def test_an_oversized_body_is_refused_before_the_application_parses_any_of_it(client):
    big = b'{"email": "' + b"a" * 1_200_000 + b'"}'
    r = client.post("/api/auth/register", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json() == {"detail": "Request body too large"}


def test_an_oversized_chunked_body_with_no_content_length_is_refused_too(client):
    def chunks():
        for _ in range(30):
            yield b"a" * 50_000

    r = client.post(
        "/api/auth/register", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 413


def test_a_normal_body_is_unaffected(client):
    r = client.post("/api/auth/register", json={**PLAIN_REQUEST, "full_name": "x" * 10_000})
    assert r.status_code != 413


def test_reads_are_never_limited(client):
    assert client.get("/health", headers={"X-Pad": "a" * 5000}).status_code == 200


def test_the_webhook_has_its_own_larger_cap_and_is_not_cut_off_by_the_global_one(client):
    body = (
        b'{"id": 1, "pad": "' + b"a" * 1_500_000 + b'"}'
    )  # over the 1 MiB global, under the 2 MB webhook cap
    r = client.post("/api/webhooks/shopify", content=body, headers={"X-Shopify-Hmac-Sha256": "x"})
    assert r.status_code == 401  # reached the signature check (and failed it), not a 413


def test_the_public_chat_api_keeps_its_own_much_smaller_cap(client):
    r = client.post("/api/public/chat/some-key/messages", content=b"a" * 40_000)
    assert r.status_code == 413


# --- cross-origin rules for the dashboard ------------------------------------------------------------------------------------


def preflight(client, origin, method="POST", headers="authorization, content-type"):
    return client.options(
        "/api/auth/me",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


def test_the_configured_dashboard_origin_may_call_with_credentials_and_the_documented_headers(
    client,
):
    r = preflight(client, "http://localhost:3000", headers="authorization, x-organization-id")
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert r.headers["access-control-allow-credentials"] == "true"


def test_another_origin_gets_no_cors_permission(client):
    r = preflight(client, "https://evil.example")
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.parametrize("headers", ["x-evil", "authorization, x-forwarded-for", "cookie"])
def test_headers_outside_the_dashboards_list_are_refused(client, headers):
    assert preflight(client, "http://localhost:3000", headers=headers).status_code == 400


def test_methods_the_dashboard_never_uses_are_refused(client):
    assert preflight(client, "http://localhost:3000", method="PUT").status_code == 400


def test_the_interactive_docs_exist_in_development(client):
    assert client.get("/openapi.json").status_code == 200


# --- the cap at the ASGI level -----------------------------------------------------------------------------------------


def _run_middleware(max_bytes, headers, chunks):
    """Drive RequestBodyLimitMiddleware directly. Returns (messages sent, receive() calls, app reached)."""
    import asyncio

    from app.core.body_limit import RequestBodyLimitMiddleware

    state = {"reads": 0, "reached": False}
    sent = []

    async def app(scope, receive, send):
        state["reached"] = True
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    pending = list(chunks)

    async def receive():
        state["reads"] += 1
        body = pending.pop(0) if pending else b"x"
        return {"type": "http.request", "body": body, "more_body": bool(pending) or not chunks}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/api/x", "headers": headers}
    asyncio.run(RequestBodyLimitMiddleware(app, max_bytes=max_bytes)(scope, receive, send))
    return sent, state["reads"], state["reached"]


def test_an_oversized_declared_body_is_refused_before_any_of_it_is_read():
    sent, reads, reached = _run_middleware(1000, [(b"content-length", b"5000")], chunks=[])
    assert sent[0]["status"] == 413 and not reached
    assert reads == 0  # not a single byte was buffered: the declaration alone was enough


def test_a_declared_length_exactly_at_the_cap_is_allowed():
    sent, reads, reached = _run_middleware(
        1000, [(b"content-length", b"1000")], chunks=[b"a" * 1000]
    )
    assert reached and sent[0]["status"] == 200


def test_an_undeclared_body_that_grows_past_the_cap_is_cut_off_as_it_arrives():
    sent, reads, reached = _run_middleware(
        1000, [], chunks=[b"a" * 400, b"a" * 400, b"a" * 400, b"a" * 400]
    )
    assert sent[0]["status"] == 413 and not reached
    assert reads == 3  # stopped at the first chunk that crossed the line, did not read the 4th
