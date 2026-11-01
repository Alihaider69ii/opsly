"""Vendor-neutral email errors (mirrors ``app/services/messaging/errors.py``).

Every provider adapter translates whatever its vendor reports into one ``EmailError`` with a
stable ``code`` from a fixed catalogue; the rest of the application only ever branches on that
code, never on a vendor's raw error shape. ``code`` and ``message`` are safe to log: they never
carry API keys or raw provider payloads.
"""

import enum


class EmailErrorCode(enum.StrEnum):
    # Configuration / policy problems caught before any provider call was made.
    UNCONFIGURED = "unconfigured"  # no usable connection/credential for this organization
    INVALID_RECIPIENT = "invalid_recipient"
    SUPPRESSED = "suppressed"  # bounce/complaint/unsubscribe kill switch
    OPTED_OUT = "opted_out"  # marketing consent not granted
    POLICY_BLOCKED = "policy_blocked"
    INVALID_REQUEST = "invalid_request"
    # Problems reported by (or on the way to) the vendor.
    AUTHENTICATION_ERROR = "authentication_error"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNKNOWN_PROVIDER_ERROR = "unknown_provider_error"


RETRYABLE = frozenset({EmailErrorCode.RATE_LIMITED, EmailErrorCode.PROVIDER_UNAVAILABLE})
POLICY = frozenset(
    {EmailErrorCode.SUPPRESSED, EmailErrorCode.OPTED_OUT, EmailErrorCode.POLICY_BLOCKED}
)


class EmailError(Exception):
    """An email provider call (or the policy gate in front of it) failed."""

    def __init__(
        self, code: EmailErrorCode | str, message: str = "", *, provider: str = ""
    ) -> None:
        self.code = EmailErrorCode(code)
        self.provider = provider
        super().__init__(message or self.code.value)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    @property
    def is_policy_block(self) -> bool:
        return self.code in POLICY


DEFAULT_MESSAGES: dict[EmailErrorCode, str] = {
    EmailErrorCode.UNCONFIGURED: "no email connection is configured for this organization",
    EmailErrorCode.INVALID_RECIPIENT: "the recipient email address is invalid",
    EmailErrorCode.SUPPRESSED: "the recipient is suppressed (bounce/complaint/unsubscribe)",
    EmailErrorCode.OPTED_OUT: "the recipient has not opted in to marketing email",
    EmailErrorCode.POLICY_BLOCKED: "sending was blocked by an email policy rule",
    EmailErrorCode.INVALID_REQUEST: "the message content or template variables are invalid",
    EmailErrorCode.AUTHENTICATION_ERROR: "the email provider rejected the credentials",
    EmailErrorCode.RATE_LIMITED: "the email provider is rate limiting requests",
    EmailErrorCode.PROVIDER_UNAVAILABLE: "the email provider is unavailable",
    EmailErrorCode.UNKNOWN_PROVIDER_ERROR: "the email provider returned an unexpected response",
}


def safe_message(code: EmailErrorCode) -> str:
    return DEFAULT_MESSAGES[code]
