"""HTTP plumbing for the Resend adapter: bounded retries with capped backoff (honouring
``Retry-After``), and normalization of every failure to an ``EmailError``.

Deliberately not shared with ``app.services.messaging.providers._http``: the two normalize into
different error vocabularies, and genericizing an existing, already-tested module across unrelated
packages is bigger surgery than Phase 9 calls for (same rationale as that module's own docstring).
The safety properties are the same proven pattern: a size-capped read, bounded attempts, and a
response body used only to classify an error, never logged or kept.
"""

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from app.services.email.errors import EmailError, EmailErrorCode, safe_message

logger = logging.getLogger(__name__)

MAX_RETRY_AFTER_SECONDS = 10.0
MAX_ERROR_BODY_BYTES = 65_536
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


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


def classify_http_status(status: int) -> EmailErrorCode:
    if status in (401, 403):
        return EmailErrorCode.AUTHENTICATION_ERROR
    if status == 429:
        return EmailErrorCode.RATE_LIMITED
    if status >= 500:
        return EmailErrorCode.PROVIDER_UNAVAILABLE
    if status == 400 or status == 422:
        return EmailErrorCode.INVALID_REQUEST
    return EmailErrorCode.UNKNOWN_PROVIDER_ERROR


class Endpoint:
    """An HTTP client for one provider base URL, with bounded retries and error normalization."""

    def __init__(
        self,
        *,
        provider_id: str,
        api_key: str,
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
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            trust_env=False,
            follow_redirects=False,
        )
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap

    def error(self, code: EmailErrorCode) -> EmailError:
        return EmailError(code, safe_message(code), provider=self.provider_id)

    def _exchange(self, method: str, path: str, payload: Any) -> httpx.Response | None:
        with self.client.stream(method, path, json=payload) as raw:
            limit = MAX_RESPONSE_BYTES if raw.status_code < 300 else MAX_ERROR_BODY_BYTES
            chunks: list[bytes] = []
            size = 0
            for chunk in raw.iter_bytes():
                size += len(chunk)
                if size > limit:
                    if raw.status_code < 300:
                        return None
                    break
                chunks.append(chunk)
            return httpx.Response(raw.status_code, headers=raw.headers, content=b"".join(chunks))

    def post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        return self._request("POST", path, payload)

    def get(self, path: str) -> httpx.Response:
        return self._request("GET", path, None)

    def _request(self, method: str, path: str, payload: Any) -> httpx.Response:
        last = self.error(EmailErrorCode.PROVIDER_UNAVAILABLE)
        for attempt in range(1, self._max_attempts + 1):
            delay: float | None = None
            try:
                response = self._exchange(method, path, payload)
                if response is None:
                    raise self.error(EmailErrorCode.UNKNOWN_PROVIDER_ERROR)
            except httpx.TimeoutException:
                last = self.error(EmailErrorCode.PROVIDER_UNAVAILABLE)
            except httpx.HTTPError:
                last = self.error(EmailErrorCode.PROVIDER_UNAVAILABLE)
            else:
                if response.status_code < 300:
                    return response
                code = classify_http_status(response.status_code)
                last = self.error(code)
                logger.warning(
                    "email request failed provider=%s status=%d code=%s attempt=%d",
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

    def close(self) -> None:
        self.client.close()
