"""Email sending capability: the vendor-neutral request/response model and the ``EmailProvider``
interface (mirrors ``app/services/messaging/provider.py``).

The send policy gate (``app/services/email/send.py``) builds a ``SendRequest`` and reads a
``SendResult``; it never sees a vendor SDK object, an HTTP response or a vendor-specific error.
Supporting another provider means writing one adapter class that translates ``SendRequest`` into
that vendor's call and back, and registering it in ``registry.py`` -- nothing upstream changes.

The provider is deliberately given no policy knowledge: it does not know about consent,
suppression, or template rendering. Those are deterministic checks/transforms the caller runs
before ever constructing a ``SendRequest``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.services.email.errors import EmailError, EmailErrorCode

__all__ = [
    "EmailError",
    "EmailErrorCode",
    "EmailProvider",
    "InboundEmailContent",
    "SendRequest",
    "SendResult",
]


@dataclass(frozen=True)
class SendRequest:
    """What the application asks an email provider to deliver. Already fully validated,
    sanitized and policy-approved by the time it reaches a provider: the provider's job is
    transport only."""

    to: str
    from_address: str
    subject: str
    idempotency_key: str
    text_body: str = ""
    html_body: str = ""
    from_name: str = ""
    reply_to: str = ""
    cc: Sequence[str] = field(default_factory=tuple)
    headers: Mapping[str, str] = field(default_factory=dict)  # In-Reply-To / References only
    # Credentials and routing, resolved by the caller (never read from application config here).
    api_key: str = field(default="", repr=False)
    base_url: str = ""


@dataclass(frozen=True)
class SendResult:
    """Acceptance by the provider's API. This is NOT delivery -- see ``docs/whatsapp.md``'s
    acceptance/sent/delivered distinction, which applies identically here."""

    provider_message_id: str
    provider: str  # catalog provider id, e.g. "resend", "fake"


@dataclass(frozen=True)
class InboundEmailContent:
    """The full body/headers of a received email. Providers whose inbound webhook carries only
    metadata (e.g. Resend) require a follow-up fetch by the webhook's reported id; this is that
    fetch's normalized result. ``html_body`` is the RAW provider HTML -- the caller
    (``services/email/webhooks.py``) sanitizes it before it is ever stored or displayed."""

    text_body: str
    html_body: str
    message_id_header: str | None = None
    in_reply_to_header: str | None = None
    references_header: str | None = None


class EmailProvider(Protocol):
    @property
    def name(self) -> str:
        """The catalog provider id this instance serves."""
        ...

    def send(self, request: SendRequest) -> SendResult:
        """Send one email. Raises ``EmailError`` on any failure."""
        ...

    def fetch_inbound(self, *, provider_message_id: str) -> InboundEmailContent:
        """Retrieve the full content of a received email the webhook only announced by id.
        Raises ``EmailError`` on any failure."""
        ...
