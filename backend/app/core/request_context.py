"""A request id for every request, in the response and in every log line it causes.

When a user reports "it failed", the answer is one string: the ``X-Request-ID`` response header
(also in the body of every 5xx). It is searchable in the logs because a logging filter stamps the
same id on each record emitted while the request is being handled. Inbound ids are honoured only if
they are short and made of harmless characters, so a caller cannot inject log lines or huge values.
"""

import logging
import re
import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = "x-request-id"
_VALID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_current: ContextVar[str | None] = ContextVar("bizflow_request_id", default=None)


def current_request_id() -> str | None:
    return _current.get()


def set_request_id(value: str | None) -> None:
    """For code that runs outside a request (worker jobs) to correlate its own log lines."""
    _current.set(value)


class RequestIdFilter(logging.Filter):
    """Adds ``record.request_id`` and ``record.request_suffix`` (`` request_id=<id>`` inside a
    request, empty outside) so a log format can end every line with the id."""

    def filter(self, record: logging.LogRecord) -> bool:
        request_id = _current.get()
        record.request_id = request_id or "-"
        record.request_suffix = f" request_id={request_id}" if request_id else ""
        return True


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        inbound = dict(scope.get("headers") or []).get(HEADER.encode(), b"").decode("latin-1")
        request_id = inbound if _VALID.match(inbound) else uuid.uuid4().hex
        token = _current.set(request_id)
        # The context variable is reset on the way out, before an outer error handler runs, so the
        # id is also kept on the request itself for handlers that answer a 5xx.
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _current.reset(token)
