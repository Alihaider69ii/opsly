"""The HTTP plumbing shared by every adapter: one client per endpoint, bounded retries with
capped backoff (honouring ``Retry-After``), and normalization of every failure to an ``AIError``.

Nothing here knows a vendor. An adapter says how the API key is presented (``Authorization:
Bearer`` by default, or a named header such as Anthropic's ``x-api-key``) and which fixed headers
the protocol needs; the response is then classified by status code (and, only for classification,
the vendor's error code). Bodies, prompts and keys are never logged.
"""

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from app.services.ai.errors import (
    AIError,
    AIErrorCode,
    classify_http_status,
    safe_message,
)

logger = logging.getLogger(__name__)

MAX_RETRY_AFTER_SECONDS = 10.0
MAX_ERROR_BODY_BYTES = 65_536
# A chat reply is a few KB and a batch of embeddings a few MB; 32 MB is far beyond either and still
# bounds what a misbehaving gateway can make a worker hold in memory.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
# Headers describing the wire encoding of a body we have already decoded and re-wrapped.
_WIRE_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


def safe_json(response: httpx.Response) -> Mapping[str, object] | None:
    """The error body as a dict, for classification only; None if it is not small JSON."""
    try:
        if len(response.content) > MAX_ERROR_BODY_BYTES:
            return None
        body = response.json()
    except (ValueError, httpx.HTTPError):
        return None
    return body if isinstance(body, dict) else None


def retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None  # HTTP-date form: fall back to the normal backoff
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class Endpoint:
    """An HTTP client for one provider endpoint, with bounded retries and error normalization."""

    def __init__(
        self,
        *,
        provider_id: str,
        api_key: str | None,
        base_url: str,
        timeout: float,
        transport: httpx.BaseTransport | None,
        sleep: Callable[[float], None],
        max_attempts: int,
        backoff_base: float,
        backoff_cap: float,
        error_cls: type[AIError],
        auth_header: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        headers = {"Content-Type": "application/json"}
        if api_key:
            if auth_header is None:
                headers["Authorization"] = f"Bearer {api_key}"
            else:
                headers[auth_header] = api_key
        headers.update(extra_headers or {})
        self.provider_id = provider_id
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers=headers,
            trust_env=False,  # never pick up proxy settings from the environment
            follow_redirects=False,
        )
        self._timeout = timeout
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._error_cls = error_cls

    def error(self, code: AIErrorCode, status: int | None = None) -> AIError:
        return self._error_cls(code, safe_message(code, status), provider=self.provider_id)

    def _exchange(self, path: str, payload: dict[str, Any]) -> httpx.Response | None:
        """One request. The body is read under a size cap (counting decoded bytes, so a compression
        bomb cannot exhaust memory) and a wall-clock deadline (the per-read timeout alone lets a
        server that drips one byte at a time hold a worker for ever). ``None`` means the success
        body was too large; an oversized error body is simply cut short, status alone classifies it.
        """
        with self.client.stream("POST", path, json=payload) as raw:
            limit = MAX_RESPONSE_BYTES if raw.status_code < 300 else MAX_ERROR_BODY_BYTES
            deadline = time.monotonic() + 2 * self._timeout
            chunks: list[bytes] = []
            size = 0
            for chunk in raw.iter_bytes():
                size += len(chunk)
                if size > limit:
                    if raw.status_code < 300:
                        return None
                    break
                if time.monotonic() > deadline:
                    raise httpx.ReadTimeout("the response body arrived too slowly")
                chunks.append(chunk)
            headers = {k: v for k, v in raw.headers.items() if k.lower() not in _WIRE_HEADERS}
            return httpx.Response(raw.status_code, headers=headers, content=b"".join(chunks))

    def post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        last = self.error(AIErrorCode.UNAVAILABLE)
        for attempt in range(1, self._max_attempts + 1):
            delay: float | None = None
            try:
                response = self._exchange(path, payload)
                if response is None:
                    raise self.error(AIErrorCode.PROVIDER_ERROR)  # not retryable: it would repeat
            except httpx.TimeoutException:
                last = self.error(AIErrorCode.TIMEOUT)
            except httpx.HTTPError:
                last = self.error(AIErrorCode.UNAVAILABLE)
            else:
                if response.status_code < 300:
                    return response
                code = classify_http_status(response.status_code, safe_json(response))
                last = self.error(code, response.status_code)
                logger.warning(
                    "ai request failed provider=%s status=%d code=%s attempt=%d",
                    self.provider_id,
                    response.status_code,
                    code.value,
                    attempt,
                )
                if not last.retryable:
                    raise last
                delay = retry_after(response)
            if attempt < self._max_attempts:
                self._sleep(
                    delay
                    if delay is not None
                    else min(self._backoff_base * 2 ** (attempt - 1), self._backoff_cap)
                )
        raise last
