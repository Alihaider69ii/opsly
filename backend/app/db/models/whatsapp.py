"""WhatsApp Business Platform integration tables: one connection per organization, its contacts,
consent/suppression state, conversations and messages, synced templates, and the inbound-webhook
dedupe ledger. All tenant-owned; children are tied to their parent's organization through composite
foreign keys, so a row can never point across tenants (mirrors ``app/db/models/shopify.py``).

Scope decisions for Phase 6 (see docs/phase-6-progress.md for the full rationale):

* One WhatsApp phone number per organization (same simplification as one Shopify store per
  organization); its fields live directly on ``WhatsAppConnection`` rather than a separate table.
* Consent is a per-category *current state* row (no separate history table): ``captured_at`` /
  ``revoked_at`` / ``metadata`` on the row already prove how the current state was reached. A
  missing row means UNKNOWN; UNKNOWN is never treated as opted in.
* Suppression is a distinct, broader kill switch (e.g. a "STOP" reply or a hard failure), checked
  before every send regardless of per-category consent.
* Dashboard sends are not a Phase 6 feature (kept minimal per the brief); every outbound message is
  created by the automation engine, so its idempotency key is always derived from
  ``(execution_id, step_index)``.

Secrets: the access token is stored only as authenticated ciphertext (``app.core.secrets``) and is
never returned by any API.
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.shopify import WebhookEventStatus  # reused: identical ledger semantics
from app.db.models.support import SenderType  # noqa: F401 (re-exported for schema/API convenience)


class WhatsAppConnectionStatus(enum.StrEnum):
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"  # needs attention, usually a rejected/expired token
    DISCONNECTED = "DISCONNECTED"


class WhatsAppConversationStatus(enum.StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class WhatsAppMessageDirection(enum.StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class WhatsAppMessageType(enum.StrEnum):
    TEXT = "TEXT"
    TEMPLATE = "TEMPLATE"
    MEDIA = "MEDIA"  # reserved: no media is sent or downloaded in Phase 6


class WhatsAppMessageStatus(enum.StrEnum):
    """Outbound lifecycle is QUEUED -> SENDING -> SENT -> DELIVERED -> READ, with FAILED/CANCELLED
    terminal at any point. RECEIVED is the only status an inbound row ever has. Status transitions
    are enforced by rank (see ``services/messaging/whatsapp/state.py``) so an out-of-order or
    duplicate webhook can never regress a message (READ must never become DELIVERED)."""

    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RECEIVED = "RECEIVED"


class WhatsAppConsentCategory(enum.StrEnum):
    TRANSACTIONAL = "TRANSACTIONAL"
    MARKETING = "MARKETING"
    SUPPORT = "SUPPORT"


class WhatsAppConsentStatus(enum.StrEnum):
    """UNKNOWN is never stored: the absence of a row for a (contact, category) pair IS unknown.
    A row exists only once a contact has explicitly opted in or opted out."""

    OPTED_IN = "OPTED_IN"
    OPTED_OUT = "OPTED_OUT"


class WhatsAppAIState(enum.StrEnum):
    """Phase 8: who is allowed to answer the customer right now, enforced by
    ``services/messaging/whatsapp/handoff.py`` before any AI reply is attempted (not by the AI
    itself -- the AI never decides this).

    AI_ACTIVE -> HUMAN_REQUIRED (a trigger fires: explicit request, repeated fallback, max AI
    replies reached) -> HUMAN_ACTIVE (a human claims it, optional) -> RESOLVED (a human is done) ->
    AI_ACTIVE (explicit resolve, see the dashboard endpoint). AI auto-reply is skipped for every
    state except AI_ACTIVE.
    """

    AI_ACTIVE = "AI_ACTIVE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    RESOLVED = "RESOLVED"


class WhatsAppAIConfidence(enum.StrEnum):
    """A deterministic bucket derived from retrieval signal (top similarity score, chunk count),
    never a model-reported number -- see ``services/ai/whatsapp_agent.py``. NONE means no
    retrieval signal existed at all (e.g. a greeting that never reached the knowledge base)."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


def _enum(cls: type[enum.Enum], name: str, length: int = 20) -> Enum:
    return Enum(cls, native_enum=False, length=length, create_constraint=True, name=name)


class WhatsAppConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One organization's link to one WhatsApp Business phone number."""

    __tablename__ = "whatsapp_connections"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_whatsapp_connections_id_org"),
        # One WhatsApp number per organization for now (same simplification as Shopify).
        UniqueConstraint("organization_id", name="uq_whatsapp_connections_organization_id"),
        # A phone number id can be linked to at most one organization while connected; a
        # DISCONNECTED row releases the claim so another organization may connect it later.
        Index(
            "uq_whatsapp_connections_active_phone",
            "phone_number_id",
            unique=True,
            postgresql_where=text("status <> 'DISCONNECTED'"),
            sqlite_where=text("status <> 'DISCONNECTED'"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[WhatsAppConnectionStatus] = mapped_column(
        _enum(WhatsAppConnectionStatus, "whatsapp_connection_status"),
        default=WhatsAppConnectionStatus.CONNECTED,
        index=True,
    )
    # Meta identifiers. phone_number_id is what every send and webhook addresses.
    phone_number_id: Mapped[str] = mapped_column(String(64))
    waba_id: Mapped[str] = mapped_column(String(64))
    business_id: Mapped[str | None] = mapped_column(String(64))
    # Normalized E.164, as reported by Meta at connect/verify time (display only).
    display_phone_number: Mapped[str | None] = mapped_column(String(32))
    verified_name: Mapped[str | None] = mapped_column(String(255))
    quality_rating: Mapped[str | None] = mapped_column(String(20))
    # Authenticated ciphertext (bf1:<key id>:...). NULL once disconnected. Never serialized.
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    token_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    api_version: Mapped[str] = mapped_column(String(20))
    # ISO 3166-1 alpha-2. Required before any ambiguous (non-E.164) phone input can be normalized
    # for this organization; a missing default fails closed rather than guessing a country.
    default_country: Mapped[str | None] = mapped_column(String(2))
    # Tenant-level safety switch (AI_AUTO_REPLY_ENABLED). Default off.
    ai_auto_reply_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # Phase 8 AI conversation policy, all organization-configurable (see docs/phase-8-progress.md
    # "AI Settings"). None means "use the deployment default" (app.core.config.Settings).
    ai_grounding_required: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    ai_min_confidence: Mapped[WhatsAppAIConfidence] = mapped_column(
        _enum(WhatsAppAIConfidence, "whatsapp_ai_confidence"),
        default=WhatsAppAIConfidence.MEDIUM,
        server_default="MEDIUM",
    )
    ai_handoff_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    ai_fallback_message: Mapped[str | None] = mapped_column(String(1000))
    ai_handoff_message: Mapped[str | None] = mapped_column(String(1000))
    # 0 disables the cap entirely; enforced per customer-service window (see
    # services/messaging/whatsapp/window.py), not per conversation lifetime.
    ai_max_replies_per_window: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Safe, user-facing failure information from a fixed catalogue (never raw exception text).
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(String(500))


class WhatsAppContact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One phone number BizFlow may message for one organization."""

    __tablename__ = "whatsapp_contacts"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_whatsapp_contacts_id_org"),
        UniqueConstraint("organization_id", "phone_e164", name="uq_whatsapp_contacts_org_phone"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    # Canonical E.164 form (e.g. "+14155551234"). The only form ever matched or queried on.
    phone_e164: Mapped[str] = mapped_column(String(20))
    # Meta's contact identifier (seen once the contact has messaged or been messaged). Not a secret.
    wa_id: Mapped[str | None] = mapped_column(String(32))
    display_name: Mapped[str | None] = mapped_column(String(255))


class WhatsAppSuppression(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A tenant-safe kill switch. While a row exists for a contact, no outbound WhatsApp message of
    any category may be sent to them, regardless of per-category consent. Checked before every
    send; never relies on workflow-level filtering alone."""

    __tablename__ = "whatsapp_suppressions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["contact_id", "organization_id"],
            ["whatsapp_contacts.id", "whatsapp_contacts.organization_id"],
            ondelete="CASCADE",
            name="fk_whatsapp_suppressions_contact_org",
        ),
        UniqueConstraint(
            "organization_id", "contact_id", name="uq_whatsapp_suppressions_org_contact"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    reason: Mapped[str] = mapped_column(String(60))
    source: Mapped[str] = mapped_column(String(60))
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WhatsAppConsent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Current consent state for one (contact, category) pair. See module docstring: UNKNOWN is
    the absence of a row, never a stored value."""

    __tablename__ = "whatsapp_consents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["contact_id", "organization_id"],
            ["whatsapp_contacts.id", "whatsapp_contacts.organization_id"],
            ondelete="CASCADE",
            name="fk_whatsapp_consents_contact_org",
        ),
        UniqueConstraint(
            "organization_id",
            "contact_id",
            "category",
            name="uq_whatsapp_consents_org_contact_category",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    category: Mapped[WhatsAppConsentCategory] = mapped_column(
        _enum(WhatsAppConsentCategory, "whatsapp_consent_category")
    )
    status: Mapped[WhatsAppConsentStatus] = mapped_column(
        _enum(WhatsAppConsentStatus, "whatsapp_consent_status")
    )
    # Short fixed code proving provenance, e.g. "whatsapp_keyword", "dashboard_manual", "api".
    source: Mapped[str] = mapped_column(String(60))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Safe provenance detail only (e.g. the inbound message id that triggered an opt-in keyword).
    # Never raw message text or request headers.
    safe_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class WhatsAppConversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One contact's thread with one connection. Carries the customer-service window state and
    (Phase 8) the human-handoff state."""

    __tablename__ = "whatsapp_conversations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["contact_id", "organization_id"],
            ["whatsapp_contacts.id", "whatsapp_contacts.organization_id"],
            ondelete="CASCADE",
            name="fk_whatsapp_conversations_contact_org",
        ),
        ForeignKeyConstraint(
            ["connection_id", "organization_id"],
            ["whatsapp_connections.id", "whatsapp_connections.organization_id"],
            ondelete="CASCADE",
            name="fk_whatsapp_conversations_connection_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_whatsapp_conversations_id_org"),
        UniqueConstraint(
            "organization_id",
            "contact_id",
            "connection_id",
            name="uq_whatsapp_conversations_org_contact_connection",
        ),
        # The dashboard's "needs a human" filter: conversations for this organization not in
        # AI_ACTIVE, newest first.
        Index("ix_whatsapp_conversations_org_ai_state", "organization_id", "ai_state"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    status: Mapped[WhatsAppConversationStatus] = mapped_column(
        _enum(WhatsAppConversationStatus, "whatsapp_conversation_status"),
        default=WhatsAppConversationStatus.OPEN,
    )
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Denormalized last_inbound_at + 24h, refreshed whenever last_inbound_at is. Eligibility is
    # always re-checked live against this at send time, never trusted as a cached boolean.
    window_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Phase 8 human handoff state machine (see WhatsAppAIState). Enforced by
    # services/messaging/whatsapp/handoff.py before any AI reply is attempted.
    ai_state: Mapped[WhatsAppAIState] = mapped_column(
        _enum(WhatsAppAIState, "whatsapp_ai_state"),
        default=WhatsAppAIState.AI_ACTIVE,
        server_default="AI_ACTIVE",
    )
    # Short fixed code, e.g. "customer_requested_human", "max_replies_reached",
    # "repeated_fallback". Never raw message text.
    handoff_reason: Mapped[str | None] = mapped_column(String(60))
    handoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Reset to 0 whenever a new customer-service window opens (see window.record_inbound); counts
    # AI auto-replies sent within the *current* window, enforcing ai_max_replies_per_window.
    ai_replies_in_window: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Consecutive AI replies that fell back (ungrounded/low-confidence) with no grounded reply in
    # between; reset to 0 on a grounded send. A repeated-failure handoff trigger (see handoff.py).
    ai_consecutive_fallbacks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class WhatsAppMessage(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One inbound or outbound WhatsApp message.

    ``idempotency_key`` is the idempotency boundary for outbound sends: the row is created
    (QUEUED) and committed *before* any provider call, and claimed with an atomic
    ``UPDATE ... WHERE status = 'QUEUED'`` before the call is made, so a retried automation step
    (same key) finds its existing row instead of sending twice. For inbound rows the key is
    deterministic from the provider message id, so a duplicate webhook delivery is also a no-op.
    """

    __tablename__ = "whatsapp_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            ["whatsapp_conversations.id", "whatsapp_conversations.organization_id"],
            ondelete="CASCADE",
            name="fk_whatsapp_messages_conversation_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_whatsapp_messages_id_org"),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_whatsapp_messages_org_idempotency"
        ),
        # Postgres and SQLite both treat multiple NULLs as distinct, so rows with no provider id
        # yet (not sent, or failed before the provider ever answered) never collide.
        UniqueConstraint(
            "organization_id", "provider_message_id", name="uq_whatsapp_messages_org_provider_id"
        ),
        Index(
            "ix_whatsapp_messages_org_conversation_created", "organization_id", "conversation_id"
        ),
        Index("ix_whatsapp_messages_status", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    direction: Mapped[WhatsAppMessageDirection] = mapped_column(
        _enum(WhatsAppMessageDirection, "whatsapp_message_direction")
    )
    message_type: Mapped[WhatsAppMessageType] = mapped_column(
        _enum(WhatsAppMessageType, "whatsapp_message_type")
    )
    status: Mapped[WhatsAppMessageStatus] = mapped_column(
        _enum(WhatsAppMessageStatus, "whatsapp_message_status")
    )
    idempotency_key: Mapped[str] = mapped_column(String(160))
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    content: Mapped[str | None] = mapped_column(Text)
    template_name: Mapped[str | None] = mapped_column(String(512))
    template_language: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(50))
    provider_error_detail: Mapped[str | None] = mapped_column(String(300))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Non-sensitive audit data only, never raw payloads: an automation back-reference
    # (execution/step ids), or (Phase 8, AI auto-replies) grounding/confidence/retrieval metadata
    # -- see app/services/ai/whatsapp_agent.py:AIReplyAudit. Never chain-of-thought, never a raw
    # retrieved document, never PII beyond what the dashboard already shows elsewhere.
    safe_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Phase 9: explicit attribution (CUSTOMER for inbound; AI/HUMAN/SYSTEM for outbound -- see
    # services/support/attribution.py). NULL for rows written before this column existed.
    sender_type: Mapped[SenderType | None] = mapped_column(
        Enum(SenderType, native_enum=False, length=20, create_constraint=True, name="sender_type")
    )
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    @property
    def ai_metadata(self) -> dict[str, Any] | None:
        """The ``"ai"`` sub-object of ``safe_metadata`` (grounded/confidence/sources/model/
        latency), if this message was AI-generated. Read by ``schemas.whatsapp.MessageOut`` so
        the dashboard can show it without the API returning ``safe_metadata`` verbatim."""
        return (self.safe_metadata or {}).get("ai")

    @property
    def ai_policy(self) -> dict[str, Any] | None:
        """The ``"policy"`` sub-object of ``safe_metadata`` (sent/fallback/handoff + reason),
        if this message went through the Phase 8 AI auto-reply policy gate."""
        return (self.safe_metadata or {}).get("policy")


class WhatsAppTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A template synced from (or about to be synced from) Meta. Only APPROVED templates may be
    used to send; see ``services/messaging/whatsapp/templates.py``."""

    __tablename__ = "whatsapp_templates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["connection_id", "organization_id"],
            ["whatsapp_connections.id", "whatsapp_connections.organization_id"],
            ondelete="CASCADE",
            name="fk_whatsapp_templates_connection_org",
        ),
        UniqueConstraint(
            "organization_id",
            "connection_id",
            "name",
            "language",
            name="uq_whatsapp_templates_org_connection_name_language",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    name: Mapped[str] = mapped_column(String(512))
    language: Mapped[str] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(30))
    # Free text (APPROVED | PENDING_REVIEW | REJECTED | PAUSED | DISABLED, per Meta's Business
    # Management API): kept as a string, not an enum, so a new vendor status cannot break a sync.
    status: Mapped[str] = mapped_column(String(30))
    provider_template_id: Mapped[str | None] = mapped_column(String(64))
    # Component/variable metadata as returned by Meta (header/body/footer/buttons, placeholder
    # names or positions). Used to validate a send's variables before any provider call.
    components: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WhatsAppWebhookEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Ledger of received webhook deliveries: the idempotency record, and the work item for the
    worker.

    Deliberately NOT identical to ``ShopifyWebhookEvent``: Shopify's worker re-fetches the current
    object by id, so its ledger keeps identifiers only. WhatsApp has no such endpoint -- an inbound
    message's text does not exist anywhere except the webhook delivery that carried it -- so
    ``payload`` holds the already-parsed, already-bounded normalized fields (wamid, from, type,
    text body, timestamp for a message; wamid, status, recipient, timestamp, and Meta's integer
    error code for a "failed" status, for a status update), never the raw Meta request body. It
    is pruned on the same retention schedule as the rest of this ledger and is never logged.

    ``dedupe_key`` is content-derived (WhatsApp sends no per-delivery id header): "msg:<wamid>"
    for an inbound message, "status:<wamid>:<status>:<timestamp>" for a status update. A genuine
    Meta retry resends an identical payload (same timestamp) and is caught here; a real status
    progression (a later, different timestamp) passes through and is made safe by the message
    state machine's rank check instead.
    """

    __tablename__ = "whatsapp_webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "phone_number_id", "dedupe_key", name="uq_whatsapp_webhook_events_phone_dedupe"
        ),
        Index("ix_whatsapp_webhook_events_status_received", "status", "received_at"),
    )

    phone_number_id: Mapped[str] = mapped_column(String(64))
    dedupe_key: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(20))  # "message" | "status"
    # NULL when phone_number_id matches no (or no longer connected) connection.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    resource_wamid: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[WebhookEventStatus] = mapped_column(
        _enum(WebhookEventStatus, "whatsapp_webhook_event_status"),
        default=WebhookEventStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(50))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
