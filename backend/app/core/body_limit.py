"""A request-body size cap for every endpoint that does not have a stricter one of its own.

Pydantic validates a body only after the whole of it has been read and parsed, so without a cap a
single huge ``POST /api/integrations/shopify/connect`` would be buffered in memory before the field
limits ever applied. Two paths are exempt because they enforce their own, different limits: the
public chat API (16 KB, ``core.public_http``) and the Shopify webhook (streamed with its own cap).
Declared lengths are checked first and cost nothing; undeclared (chunked) bodies are counted as they
arrive and rejected the moment they pass the limit.
"""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_EXEMPT_PREFIXES = ("/api/public/", "/api/webhooks/")


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int,
        exempt_prefixes: tuple[str, ...] = DEFAULT_EXEMPT_PREFIXES,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.exempt = exempt_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] in ("GET", "HEAD", "OPTIONS")
            or scope["path"].startswith(self.exempt)
        ):
            await self.app(scope, receive, send)
            return

        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await self._refuse(send)
            return

        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > self.max_bytes:
                await self._refuse(send)
                return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _refuse(send: Send) -> None:
        payload = json.dumps({"detail": "Request body too large"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
