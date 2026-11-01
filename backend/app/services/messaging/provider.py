"""Messaging capability: the vendor-neutral request/response model and the ``MessagingProvider``
interface (mirrors ``app/services/ai/provider.py``).

The automation engine and the WhatsApp send service build a ``SendRequest`` and read a
``SendResult``; neither ever sees a vendor SDK object, an HTTP response or a vendor-specific error.
Supporting another channel (SMS, email, Instagram, ...) means writing one adapter class that
translates ``SendRequest`` into that vendor's call and back, and registering it in
``registry.py``. Nothing in the automation engine or the policy layer changes.

The provider is deliberately given no policy knowledge: it does not know about consent, opt-outs,
the customer-service window, or whether a template is approved. Those are deterministic checks the
caller (``services/messaging/whatsapp/send.py``) runs before ever constructing a ``SendRequest``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from app.services.messaging.errors import MessagingError, MessagingErrorCode

__all__ = [
    "MessagingError",
    "MessagingErrorCode",
    "MessagingProvider",
    "SendRequest",
    "SendResult",
    "TemplateComponent",
]

MessageKind = Literal["text", "template"]


@dataclass(frozen=True)
class TemplateComponent:
    """One resolved template component to send, e.g. {"type": "body", "parameters": [...]}."""

    type: str
    parameters: Sequence[Mapping[str, str]] = field(default_factory=tuple)


@dataclass(frozen=True)
class SendRequest:
    """What the application asks a messaging provider to deliver. Already fully validated and
    policy-approved by the time it reaches a provider: the provider's job is transport only."""

    to_e164: str
    kind: MessageKind
    idempotency_key: str
    text: str | None = None  # kind == "text"
    template_name: str | None = None  # kind == "template"
    template_language: str | None = None
    template_components: Sequence[TemplateComponent] = field(default_factory=tuple)
    # Credentials and routing, resolved by the caller (never read from application config here).
    phone_number_id: str = ""
    access_token: str = field(default="", repr=False)
    api_version: str = ""
    base_url: str = ""


@dataclass(frozen=True)
class SendResult:
    """Acceptance by the provider's API. This is NOT delivery: see
    ``docs/whatsapp.md`` for the full acceptance/sent/delivered/read distinction."""

    provider_message_id: str  # the vendor's message id (e.g. a WhatsApp "wamid...")
    provider: str  # catalog provider id, e.g. "whatsapp_cloud", "fake"


class MessagingProvider(Protocol):
    @property
    def name(self) -> str:
        """The catalog provider id this instance serves."""
        ...

    def send_text(self, request: SendRequest) -> SendResult:
        """Send a free-form text message. Raises ``MessagingError`` on any failure."""
        ...

    def send_template(self, request: SendRequest) -> SendResult:
        """Send a pre-approved template message. Raises ``MessagingError`` on any failure."""
        ...

    def mark_read(
        self,
        *,
        provider_message_id: str,
        phone_number_id: str,
        access_token: str,
        api_version: str,
        base_url: str = "",
    ) -> None:
        """Best-effort read receipt for an inbound message. Raises ``MessagingError``."""
        ...
