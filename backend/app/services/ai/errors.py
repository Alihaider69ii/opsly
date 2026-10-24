"""Vendor-neutral AI errors.

Every adapter translates whatever its vendor reports (HTTP status, error body, SDK exception) into
one ``AIError`` with a stable ``code``. The rest of the application only ever branches on that code,
so it never needs to know which vendor produced an error. Messages are safe to log: they never carry
prompts, response bodies or credentials.
"""

import enum
from collections.abc import Mapping


class AIErrorCode(enum.StrEnum):
    # Configuration problems (nothing was sent to a vendor).
    UNCONFIGURED = "unconfigured"  # a required setting such as an API key is missing
    MISCONFIGURED = "misconfigured"  # a setting is present but unusable
    # Problems reported by (or on the way to) the vendor.
    AUTH = "auth"  # authentication failure: bad, expired or unauthorized key
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"  # vendor down, 5xx, or unreachable
    INVALID_REQUEST = "invalid_request"  # the vendor rejected the request itself
    MODEL_UNAVAILABLE = "model_unavailable"  # unknown model, or no access to it
    PROVIDER_ERROR = "provider_error"  # a reply the adapter cannot interpret
    CONTENT_FILTERED = "content_filtered"


RETRYABLE = frozenset({AIErrorCode.RATE_LIMITED, AIErrorCode.TIMEOUT, AIErrorCode.UNAVAILABLE})
CONFIGURATION = frozenset({AIErrorCode.UNCONFIGURED, AIErrorCode.MISCONFIGURED})


class AIError(Exception):
    """An AI provider call (or its configuration) failed. ``code`` is an ``AIErrorCode`` value."""

    def __init__(self, code: AIErrorCode | str, message: str = "", *, provider: str = "") -> None:
        self.code = AIErrorCode(code)
        self.provider = provider
        super().__init__(message or self.code.value)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    @property
    def is_configuration_problem(self) -> bool:
        return self.code in CONFIGURATION


class LLMError(AIError):
    """An LLM (text generation) call failed."""


class EmbeddingError(AIError):
    """An embedding call failed."""


def _error_code_and_text(body: Mapping[str, object] | None) -> tuple[str, str]:
    """The vendor's error ``code`` and its free text, lower-cased, for classification only. The body
    itself is never kept, logged or shown."""
    error = body.get("error") if body else None
    if isinstance(error, Mapping):
        code = str(error.get("code") or error.get("type") or "").lower()
        text = " ".join(str(error.get(k, "")) for k in ("code", "type", "message")).lower()
        return code, text
    return "", str(error or "").lower()


_MODEL_CODES = ("model_not_found", "invalid_model", "model_not_available")


def classify_http_status(status: int, body: Mapping[str, object] | None = None) -> AIErrorCode:
    """Map an HTTP status (and, only for classification, the vendor's error body) to a code.

    The OpenAI-compatible family is largely consistent here; adapters for vendors that differ
    classify in their own module and still return an ``AIErrorCode``.
    """
    code, text = _error_code_and_text(body)
    if status in (401, 402, 403):  # 402: a billing problem on the account (Anthropic billing_error)
        return AIErrorCode.AUTH
    if status == 429:
        return AIErrorCode.RATE_LIMITED
    if status in (408, 504):
        return AIErrorCode.TIMEOUT
    if status == 404:
        # An unknown model is a 404 on OpenAI; a wrong base URL is also a 404 with no model hint.
        return AIErrorCode.MODEL_UNAVAILABLE if "model" in text else AIErrorCode.MISCONFIGURED
    if status >= 500:
        return AIErrorCode.UNAVAILABLE
    if any(marker in code for marker in _MODEL_CODES):
        return (
            AIErrorCode.MODEL_UNAVAILABLE
        )  # only an explicit code: free text may merely mention one
    return AIErrorCode.INVALID_REQUEST


# Safe, generic wording for each code. Server logs may add detail; users never see any of it.
DEFAULT_MESSAGES: dict[AIErrorCode, str] = {
    AIErrorCode.AUTH: "the AI provider rejected the credentials",
    AIErrorCode.RATE_LIMITED: "the AI provider is rate limiting requests",
    AIErrorCode.TIMEOUT: "the AI provider timed out",
    AIErrorCode.UNAVAILABLE: "the AI provider is unavailable",
    AIErrorCode.INVALID_REQUEST: "the AI provider rejected the request",
    AIErrorCode.MODEL_UNAVAILABLE: "the configured model is not available from the AI provider",
    AIErrorCode.PROVIDER_ERROR: "the AI provider returned an unexpected response",
    AIErrorCode.CONTENT_FILTERED: "the AI provider filtered the response",
    AIErrorCode.MISCONFIGURED: "the AI provider endpoint was not found; check the base URL",
    AIErrorCode.UNCONFIGURED: "the AI provider is not configured",
}


def safe_message(code: AIErrorCode, status: int | None = None) -> str:
    base = DEFAULT_MESSAGES[code]
    return f"{base} (HTTP {status})" if status is not None else base
