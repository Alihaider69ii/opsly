"""Vendor-neutral messaging errors (mirrors ``app/services/ai/errors.py``).

Every provider adapter translates whatever its vendor reports into one ``MessagingError`` with a
stable ``code`` from a fixed catalogue; the rest of the application only ever branches on that
code, never on a vendor's raw error shape. ``code`` and ``message`` are safe to log: they never
carry tokens, raw provider payloads or full customer message text.
"""

import enum


class MessagingErrorCode(enum.StrEnum):
    # Configuration / policy problems caught before any provider call was made.
    UNCONFIGURED = "unconfigured"  # no usable connection/credential for this organization
    INVALID_RECIPIENT = "invalid_recipient"  # phone could not be normalized, or is not a contact
    TEMPLATE_NOT_APPROVED = "template_not_approved"
    WINDOW_CLOSED = "window_closed"  # free-form text outside the customer-service window
    OPTED_OUT = "opted_out"
    POLICY_BLOCKED = "policy_blocked"  # suppressed, or another deterministic policy rule failed
    INVALID_REQUEST = "invalid_request"  # malformed variables/content; fails closed
    # Problems reported by (or on the way to) the vendor.
    AUTHENTICATION_ERROR = "authentication_error"
    RATE_LIMITED = "rate_limited"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNKNOWN_PROVIDER_ERROR = "unknown_provider_error"


# Only transient conditions are retried: a vendor-side one (RATE_LIMITED, PROVIDER_UNAVAILABLE) or
# our own send-side rate limit (also RATE_LIMITED -- see services/messaging/whatsapp/send.py), both
# of which are expected to clear on their own. A policy or validation failure is never retried:
# resending the same content would fail the same way again, or worse, succeed after the policy
# that blocked it was meant to still apply.
RETRYABLE = frozenset({MessagingErrorCode.RATE_LIMITED, MessagingErrorCode.PROVIDER_UNAVAILABLE})
POLICY = frozenset(
    {
        MessagingErrorCode.TEMPLATE_NOT_APPROVED,
        MessagingErrorCode.WINDOW_CLOSED,
        MessagingErrorCode.OPTED_OUT,
        MessagingErrorCode.POLICY_BLOCKED,
    }
)


class MessagingError(Exception):
    """A messaging provider call (or the policy gate in front of it) failed."""

    def __init__(
        self, code: MessagingErrorCode | str, message: str = "", *, provider: str = ""
    ) -> None:
        self.code = MessagingErrorCode(code)
        self.provider = provider
        super().__init__(message or self.code.value)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    @property
    def is_policy_block(self) -> bool:
        return self.code in POLICY


DEFAULT_MESSAGES: dict[MessagingErrorCode, str] = {
    MessagingErrorCode.UNCONFIGURED: "no WhatsApp connection is configured for this organization",
    MessagingErrorCode.INVALID_RECIPIENT: "the recipient phone number is invalid",
    MessagingErrorCode.TEMPLATE_NOT_APPROVED: "the message template is not approved for sending",
    MessagingErrorCode.WINDOW_CLOSED: "the customer-service window is closed; an approved "
    "template is required",
    MessagingErrorCode.OPTED_OUT: "the recipient has opted out of this message category",
    MessagingErrorCode.POLICY_BLOCKED: "sending was blocked by a messaging policy rule",
    MessagingErrorCode.INVALID_REQUEST: "the message content or template variables are invalid",
    MessagingErrorCode.AUTHENTICATION_ERROR: "the messaging provider rejected the credentials",
    MessagingErrorCode.RATE_LIMITED: "the messaging provider is rate limiting requests",
    MessagingErrorCode.PROVIDER_UNAVAILABLE: "the messaging provider is unavailable",
    MessagingErrorCode.UNKNOWN_PROVIDER_ERROR: "the messaging provider returned an unexpected "
    "response",
}


def safe_message(code: MessagingErrorCode) -> str:
    return DEFAULT_MESSAGES[code]
