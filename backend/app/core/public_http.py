"""HTTP behavior for the public (website visitor) API: CORS, request-size cap, safe headers.

Pure ASGI middleware so it can (a) answer CORS preflights without touching the app, (b) reject
oversized bodies BEFORE the application sees any of them, and (c) replace the dashboard-oriented
CORS headers that the global CORS middleware may have added with the public ones.

Public chat is called from arbitrary customer websites, so it answers ``Access-Control-Allow-Origin:
*`` and never uses credentials (no cookies; the conversation token travels in a header). Which sites
may actually embed a given widget is decided per organization in the service layer.

The body is buffered here (it is capped at a few KB) rather than limited while the app reads it:
a framework that catches errors raised from ``receive`` would turn them into a 400, and an attacker
could even craft a body that is valid JSON up to the limit so the endpoint acts on it before the
response is replaced. Buffering first means an oversized request never reaches the app at all.
"""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

PUBLIC_PREFIX = "/api/public/"
ALLOWED_HEADERS = "Content-Type, X-Conversation-Token"
CORS_HEADERS = {
    b"access-control-allow-origin": b"*",
    b"access-control-allow-methods": b"GET, POST, OPTIONS",
    b"access-control-allow-headers": ALLOWED_HEADERS.encode(),
    b"access-control-expose-headers": b"Retry-After",
    b"access-control-max-age": b"600",
}
SAFE_HEADERS = {
    b"x-content-type-options": b"nosniff",
    b"cache-control": b"no-store",
    b"referrer-policy": b"no-referrer",
}


class PublicApiMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(PUBLIC_PREFIX):
            await self.app(scope, receive, send)
            return

        if scope["method"] == "OPTIONS":
            await self._respond(send, 204, b"", CORS_HEADERS)
            return

        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_body_bytes:
            await self._too_large(send)
            return

        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > self.max_body_bytes:
                await self._too_large(send)
                return

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                kept = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if not k.lower().startswith(b"access-control-")
                    and k.lower() not in SAFE_HEADERS
                ]
                kept.extend(CORS_HEADERS.items())
                kept.extend(SAFE_HEADERS.items())
                message = {**message, "headers": kept}
            await send(message)

        await self.app(scope, replay_receive, wrapped_send)

    async def _too_large(self, send: Send) -> None:
        body = json.dumps({"detail": "Request body is too large"}).encode()
        await self._respond(send, 413, body, {**CORS_HEADERS, b"content-type": b"application/json"})

    @staticmethod
    async def _respond(send: Send, status: int, body: bytes, headers: dict[bytes, bytes]) -> None:
        merged = {**headers, **SAFE_HEADERS, b"content-length": str(len(body)).encode()}
        await send(
            {"type": "http.response.start", "status": status, "headers": list(merged.items())}
        )
        await send({"type": "http.response.body", "body": body})
