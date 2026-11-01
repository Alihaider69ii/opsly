"""HTTP plumbing shared by messaging adapters: one client per call, bounded retries with capped
backoff (honouring ``Retry-After``), and normalization of every failure to a ``MessagingError``.

Deliberately not shared with ``app.services.ai.providers._http``: the two normalize into different
error vocabularies (``OPTED_OUT`` / ``WINDOW_CLOSED`` have no AI analog) and genericizing an
existing, already-tested module across unrelated packages is bigger surgery than Phase 6 calls
for. The safety properties are the same proven pattern: a size-capped read (so a compression bomb
or a slow drip cannot exhaust memory or hold a worker forever), bounded attempts, and a response
body that is used only to classify an error, never logged or kept.
"""

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from app.services.messaging.errors import MessagingError, MessagingErrorCode, safe_message

logger = logging.getLogger(__name__)

MAX_RETRY_AFTER_SECONDS = 10.0
MAX_ERROR_BODY_BYTES = 65_536
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_WIRE_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


def safe_json(response: httpx.Response) -> Mapping[str, object] | None:
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
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


# Meta's documented WhatsApp Cloud API error codes (not HTTP statuses): Graph API returns almost
# every business-logic error -- a permanent validation failure and a transient throughput error
# alike -- as plain HTTP 400, so ``error.code`` in the body is the only way to tell them apart.
# Verified against https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
# (live-fetched for Phase 7). Codes not listed here (e.g. template/parameter validation, the
# 24-hour-window and opt-out codes) fall through to the generic status-based classification below,
# which maps a plain 400 to INVALID_REQUEST -- correct for those, since none of them are retryable.
_AUTH_ERROR_CODES = frozenset({0, 3, 10, 190, 200})  # revoked/expired token, missing/denied scope
_RATE_LIMIT_ERROR_CODES = frozenset(
    {4, 80007, 130429, 131056}
)  # app/WABA/throughput/recipient caps
_TRANSIENT_ERROR_CODES = frozenset({1, 2})  # Meta's own "invalid request / temporary issue" codes


def _vendor_error_code(body: Mapping[str, object] | None) -> int | None:
    error = body.get("error") if body else None
    code = error.get("code") if isinstance(error, Mapping) else None
    return code if isinstance(code, int) else None


def classify_http_status(
    status: int, body: Mapping[str, object] | None = None
) -> MessagingErrorCode:
    """Map a Graph API HTTP status (and, only for classification, the parsed error body) to a
    code. The body is read only to pick ``error.code`` out of it; it is never logged or kept."""
    vendor_code = _vendor_error_code(body)
    if vendor_code in _AUTH_ERROR_CODES:
        return MessagingErrorCode.AUTHENTICATION_ERROR
    if vendor_code in _RATE_LIMIT_ERROR_CODES:
        return MessagingErrorCode.RATE_LIMITED
    if vendor_code in _TRANSIENT_ERROR_CODES:
        return MessagingErrorCode.PROVIDER_UNAVAILABLE
    if status in (401, 403):
        return MessagingErrorCode.AUTHENTICATION_ERROR
    if status == 429:
        return MessagingErrorCode.RATE_LIMITED
    if status >= 500:
        return MessagingErrorCode.PROVIDER_UNAVAILABLE
    if status == 400:
        return MessagingErrorCode.INVALID_REQUEST
    return MessagingErrorCode.UNKNOWN_PROVIDER_ERROR


class Endpoint:
    """An HTTP client for one provider base URL, with bounded retries and error normalization."""

    def __init__(
        self,
        *,
        provider_id: str,
        access_token: str,
        base_url: str,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
    ) -> None:
        self.provider_id = provider_id
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            trust_env=False,
            follow_redirects=False,
        )
        self._timeout = timeout
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap

    def error(self, code: MessagingErrorCode) -> MessagingError:
        return MessagingError(code, safe_message(code), provider=self.provider_id)

    def _exchange(
        self, method: str, path: str, *, params: Mapping[str, str] | None, payload: Any | None
    ) -> httpx.Response | None:
        with self.client.stream(method, path, params=params, json=payload) as raw:
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

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Any | None = None,
    ) -> httpx.Response:
        last = self.error(MessagingErrorCode.PROVIDER_UNAVAILABLE)
        for attempt in range(1, self._max_attempts + 1):
            delay: float | None = None
            try:
                response = self._exchange(method, path, params=params, payload=payload)
                if response is None:
                    raise self.error(MessagingErrorCode.UNKNOWN_PROVIDER_ERROR)
            except httpx.TimeoutException:
                last = self.error(MessagingErrorCode.PROVIDER_UNAVAILABLE)
            except httpx.HTTPError:
                last = self.error(MessagingErrorCode.PROVIDER_UNAVAILABLE)
            else:
                if response.status_code < 300:
                    return response
                code = classify_http_status(response.status_code, safe_json(response))
                last = self.error(code)
                logger.warning(
                    "messaging request failed provider=%s status=%d code=%s attempt=%d",
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

    def post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        return self.request("POST", path, payload=payload)

    def get(self, path: str, params: Mapping[str, str] | None = None) -> httpx.Response:
        return self.request("GET", path, params=params)

    def close(self) -> None:
        self.client.close()
