"""Phase 9: email support + automation/campaign tables.

Mirrors the WhatsApp module's shape deliberately (``app/db/models/whatsapp.py``): one connection
per organization, a thread/message pair with an idempotency key for outbound sends, and a webhook
ledger for inbound/status deliveries. Reuses ``WhatsAppConnectionStatus`` (CONNECTED/ERROR/
DISCONNECTED has no channel-specific meaning) and ``WhatsAppAIConfidence`` (the deterministic
retrieval-signal bucket is channel-agnostic) rather than redefining equivalent enums.

Threading uses RFC 5322 ``Message-ID``/``In-Reply-To``/``References`` plus the provider's own
thread id where available (see ``app/services/email/threading.py``); a thread is never merged or
split just because two messages share a sender address.

All inbound HTML is sanitized before it is stored (``body_html_sanitized``) -- the raw provider
payload is never persisted. Secrets (the provider API key) are environment-based, not stored per
row, per the Phase 9 brief; nothing here is ciphertext.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.shopify import WebhookEventStatus  # reused: identical ledger semantics
from app.db.models.support import SenderType  # noqa: F401 (re-exported for schema/API convenience)
from app.db.models.whatsapp import WhatsAppAIConfidence, WhatsAppAIState, WhatsAppConnectionStatus


class EmailDirection(enum.StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class EmailMessageStatus(enum.StrEnum):
    """QUEUED -> SENDING -> SENT -> DELIVERED, with BOUNCED/FAILED/COMPLAINT/UNSUBSCRIBED terminal
    at any point. RECEIVED is the only status an inbound row ever has. Transitions are rank-
    checked the same way WhatsApp's are (``services/email/state.py``), so an out-of-order or
    duplicate provider webhook can never regress a message."""

    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    BOUNCED = "BOUNCED"
    FAILED = "FAILED"
    COMPLAINT = "COMPLAINT"
    UNSUBSCRIBED = "UNSUBSCRIBED"
    RECEIVED = "RECEIVED"


class EmailClassification(enum.StrEnum):
    TRANSACTIONAL = "TRANSACTIONAL"
    LIFECYCLE = "LIFECYCLE"
    MARKETING = "MARKETING"


class EmailTemplateStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class EmailCampaignStatus(enum.StrEnum):
    DRAFT = "DRAFT"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class EmailCampaignRecipientStatus(enum.StrEnum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED_SUPPRESSED = "SKIPPED_SUPPRESSED"


def _enum(cls: type[enum.Enum], name: str, length: int = 20) -> Enum:
    return Enum(cls, native_enum=False, length=length, create_constraint=True, name=name)


class EmailConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One organization's email-sending identity. Provider credentials are process-wide
    (``Settings.email_provider`` / ``EMAIL_API_KEY``, see ``app/services/email/factory.py``), per
    the brief's "credentials must be environment based" -- this row holds only per-organization,
    non-secret configuration plus the Phase 9 AI auto-reply policy (mirrors
    ``WhatsAppConnection``'s ai_* columns)."""

    __tablename__ = "email_connections"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_email_connections_id_org"),
        UniqueConstraint("organization_id", name="uq_email_connections_organization_id"),
        UniqueConstraint("inbound_address", name="uq_email_connections_inbound_address"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[WhatsAppConnectionStatus] = mapped_column(
        _enum(WhatsAppConnectionStatus, "email_connection_status"),
        default=WhatsAppConnectionStatus.CONNECTED,
        index=True,
    )
    from_address: Mapped[str] = mapped_column(String(320))
    from_name: Mapped[str | None] = mapped_column(String(255))
    reply_to_address: Mapped[str | None] = mapped_column(String(320))
    # The deterministic per-organization inbound routing key (e.g. "org-<slug>@inbound.<domain>"),
    # analogous to WhatsApp's phone_number_id: inbound webhooks resolve the organization from the
    # message's recipient address, never from client input.
    inbound_address: Mapped[str] = mapped_column(String(320))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    ai_auto_reply_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    ai_grounding_required: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    ai_min_confidence: Mapped[WhatsAppAIConfidence] = mapped_column(
        _enum(WhatsAppAIConfidence, "email_ai_confidence"),
        default=WhatsAppAIConfidence.MEDIUM,
        server_default="MEDIUM",
    )
    ai_handoff_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    ai_fallback_message: Mapped[str | None] = mapped_column(String(1000))
    ai_handoff_message: Mapped[str | None] = mapped_column(String(1000))
    ai_max_replies_per_window: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(String(500))


class EmailThread(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One email conversation: everything sharing a provider thread id or RFC 5322
    ``References``/``In-Reply-To`` chain (see ``services/email/threading.py``)."""

    __tablename__ = "email_threads"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_email_threads_id_org"),
        Index("ix_email_threads_org_customer", "organization_id", "customer_id"),
        Index("ix_email_threads_org_ai_state", "organization_id", "ai_state"),
        ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="SET NULL",
            name="fk_email_threads_customer_org",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    customer_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(998))  # RFC 5322 line-length ceiling
    provider_thread_id: Mapped[str | None] = mapped_column(String(128))
    message_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Email has no WhatsApp-style time-boxed customer-service window, so these are scoped to the
    # whole thread rather than "per window" -- same cost/loop control purpose as
    # WhatsAppConversation.ai_replies_in_window / ai_consecutive_fallbacks (see
    # app/services/email/autoreply.py), same field names for a reader moving between channels.
    ai_replies_in_window: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ai_consecutive_fallbacks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Self-contained AI/human handoff state (mirrors WhatsAppConversation.ai_state exactly; see
    # app/services/email/handoff.py), authoritative for email -- enforced by
    # services/email/autoreply.py before any AI reply is attempted, never by the AI itself.
    ai_state: Mapped[WhatsAppAIState] = mapped_column(
        Enum(
            WhatsAppAIState,
            native_enum=False,
            length=20,
            create_constraint=True,
            name="email_ai_state",
        ),
        default=WhatsAppAIState.AI_ACTIVE,
        server_default="AI_ACTIVE",
    )
    handoff_reason: Mapped[str | None] = mapped_column(String(60))
    handoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailMessage(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One inbound or outbound email. ``idempotency_key`` is the outbound send idempotency
    boundary (mirrors ``WhatsAppMessage``); for inbound rows the key is derived from the
    provider's own event id, so a duplicate webhook delivery is also a no-op."""

    __tablename__ = "email_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["thread_id", "organization_id"],
            ["email_threads.id", "email_threads.organization_id"],
            ondelete="CASCADE",
            name="fk_email_messages_thread_org",
        ),
        ForeignKeyConstraint(
            ["template_id", "organization_id"],
            ["email_templates.id", "email_templates.organization_id"],
            ondelete="SET NULL",
            name="fk_email_messages_template_org",
        ),
        ForeignKeyConstraint(
            ["campaign_id", "organization_id"],
            ["email_campaigns.id", "email_campaigns.organization_id"],
            ondelete="SET NULL",
            name="fk_email_messages_campaign_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_email_messages_id_org"),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_email_messages_org_idempotency"
        ),
        UniqueConstraint(
            "organization_id", "provider_message_id", name="uq_email_messages_org_provider_id"
        ),
        Index("ix_email_messages_org_thread_created", "organization_id", "thread_id"),
        Index("ix_email_messages_org_message_id_header", "organization_id", "message_id_header"),
        Index("ix_email_messages_status", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    direction: Mapped[EmailDirection] = mapped_column(_enum(EmailDirection, "email_direction"))
    status: Mapped[EmailMessageStatus] = mapped_column(
        _enum(EmailMessageStatus, "email_message_status")
    )
    idempotency_key: Mapped[str] = mapped_column(String(160))
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    message_id_header: Mapped[str | None] = mapped_column(String(998))
    in_reply_to_header: Mapped[str | None] = mapped_column(String(998))
    references_header: Mapped[str | None] = mapped_column(Text)
    from_address: Mapped[str] = mapped_column(String(320))
    to_addresses: Mapped[list[str]] = mapped_column(JSON, default=list)
    cc_addresses: Mapped[list[str]] = mapped_column(JSON, default=list)
    subject: Mapped[str | None] = mapped_column(String(998))
    body_text: Mapped[str | None] = mapped_column(Text)
    # Sanitized before this row is ever written -- see services/email/sanitize.py. The raw
    # provider payload is never persisted.
    body_html_sanitized: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[EmailClassification | None] = mapped_column(
        _enum(EmailClassification, "email_classification")
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    error_code: Mapped[str | None] = mapped_column(String(50))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Phase 9 attribution (see app/db/models/support.py:SenderType) + non-sensitive audit data:
    # an automation/campaign back-reference, or AI grounding/confidence/retrieval metadata --
    # same shape as WhatsAppMessage.safe_metadata. Never raw provider payloads, never secrets.
    sender_type: Mapped[SenderType | None] = mapped_column(
        Enum(SenderType, native_enum=False, length=20, create_constraint=True, name="sender_type")
    )
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    safe_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    complained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def ai_metadata(self) -> dict[str, Any] | None:
        return (self.safe_metadata or {}).get("ai")


class EmailTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "email_templates"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_email_templates_id_org"),
        Index("ix_email_templates_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    type: Mapped[EmailClassification] = mapped_column(
        _enum(EmailClassification, "email_template_type")
    )
    subject: Mapped[str] = mapped_column(String(998))
    preheader: Mapped[str | None] = mapped_column(String(200))
    body_html: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    # Declared placeholder names (e.g. ["customer.first_name", "order.number"]); an unknown
    # variable at render time fails safely rather than being substituted -- see
    # services/email/templates.py.
    variables: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[EmailTemplateStatus] = mapped_column(
        _enum(EmailTemplateStatus, "email_template_status"), default=EmailTemplateStatus.DRAFT
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )


class EmailCampaign(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "email_campaigns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["template_id", "organization_id"],
            ["email_templates.id", "email_templates.organization_id"],
            name="fk_email_campaigns_template_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_email_campaigns_id_org"),
        Index("ix_email_campaigns_org_status", "organization_id", "status"),
        Index("ix_email_campaigns_status_scheduled", "status", "scheduled_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    # A bounded filter spec (e.g. {"marketing_consent": true, "tags": ["vip"]}), validated and
    # applied by services/email/campaigns.py -- never raw SQL, never arbitrary code.
    audience_definition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[EmailCampaignStatus] = mapped_column(
        _enum(EmailCampaignStatus, "email_campaign_status"), default=EmailCampaignStatus.DRAFT
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    total_recipients: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sent_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class EmailCampaignRecipient(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One (campaign, customer) send attempt. The unique constraint IS the idempotency guarantee
    the brief requires: "a worker retry must never send duplicate campaign email to the same
    recipient" -- a retry claims this existing row (``UPDATE ... WHERE status = 'PENDING'``,
    mirroring ``AutomationExecutionStep``) rather than creating a second one."""

    __tablename__ = "email_campaign_recipients"
    __table_args__ = (
        ForeignKeyConstraint(
            ["campaign_id", "organization_id"],
            ["email_campaigns.id", "email_campaigns.organization_id"],
            ondelete="CASCADE",
            name="fk_email_campaign_recipients_campaign_org",
        ),
        UniqueConstraint(
            "organization_id",
            "campaign_id",
            "customer_id",
            name="uq_email_campaign_recipients_org_campaign_customer",
        ),
        Index("ix_email_campaign_recipients_status", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    email: Mapped[str] = mapped_column(String(320))
    status: Mapped[EmailCampaignRecipientStatus] = mapped_column(
        _enum(EmailCampaignRecipientStatus, "email_campaign_recipient_status"),
        default=EmailCampaignRecipientStatus.PENDING,
    )
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    error_code: Mapped[str | None] = mapped_column(String(50))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailWebhookEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Ledger of received provider webhook deliveries (inbound mail, or a delivery/bounce/
    complaint/unsubscribe status event). Mirrors ``WhatsAppWebhookEvent``: ``payload`` holds only
    already-parsed, already-bounded normalized fields, never the raw provider request body.

    ``dedupe_key`` is globally unique (unlike WhatsApp's per-phone-number scoping): a provider's
    own event id, or an inbound message's ``Message-ID`` header, is unique on its own.
    """

    __tablename__ = "email_webhook_events"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_email_webhook_events_dedupe"),)

    dedupe_key: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(20))  # "inbound" | "status"
    # NULL until the recipient/connection is resolved (an inbound to an unknown address).
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[WebhookEventStatus] = mapped_column(
        _enum(WebhookEventStatus, "email_webhook_event_status"), default=WebhookEventStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(50))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
