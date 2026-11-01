"""Phase 9: the unified, cross-channel support layer.

This module does NOT replace the channel-specific conversation/message tables (``Conversation``/
``Message`` for website chat, ``WhatsAppConversation``/``WhatsAppMessage`` for WhatsApp, and
``EmailThread``/``EmailMessage`` for email, see ``app/db/models/email.py``). Those keep their own
schemas, idempotency keys and (for WhatsApp) the Phase 8 AI/handoff state machine unchanged --
rewriting them would risk the exact regressions Phase 9's brief forbids.

Instead ``SupportConversation`` is a thin, tenant-owned INDEX row: one per underlying channel
conversation, created/kept in sync by whichever channel's intake code processes a message (website
``services/chat/service.py``, WhatsApp ``services/messaging/whatsapp/webhooks.py``, email
``services/email/webhooks.py`` and ``services/email/send.py``). It carries only the fields that do
not belong on any single channel's table: priority, human assignment, and a normalized status/
AI-state view for dashboard filtering across channels.

For WhatsApp, ``WhatsAppConversation.ai_state`` remains authoritative (``autoreply.py`` enforces it
directly, unchanged) -- this row's ``ai_state`` is a mirror, kept in sync by the same code paths
that already call ``services/messaging/whatsapp/handoff.py``. For website and email, which have no
prior human-handoff concept, this row's ``ai_state`` IS the source of truth: the website public-chat
send path and the email auto-reply path both consult it before ever calling the AI.

``SupportTimelineEvent`` is the one mechanism backing three brief requirements at once: the
customer timeline, the support audit trail, and the consent/suppression change audit -- a single
append-only, paginated ledger rather than three separate ones.
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
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin


class SupportChannel(enum.StrEnum):
    WEBSITE = "WEBSITE"
    WHATSAPP = "WHATSAPP"
    EMAIL = "EMAIL"


class SupportStatus(enum.StrEnum):
    """A normalized, cross-channel view for dashboard filtering. Derived from (and kept in sync
    with) each channel's own status/ai_state -- never a second independent state machine."""

    OPEN = "OPEN"
    AI_ACTIVE = "AI_ACTIVE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    WAITING = "WAITING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class SupportPriority(enum.StrEnum):
    URGENT = "URGENT"
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


class PrioritySource(enum.StrEnum):
    SYSTEM = "SYSTEM"
    AI = "AI"
    HUMAN = "HUMAN"


class AIHandoffState(enum.StrEnum):
    """Mirrors ``WhatsAppAIState`` (same values, so the dashboard treats every channel
    uniformly) but is a distinct enum: WhatsApp's own column/table is untouched by Phase 9."""

    AI_ACTIVE = "AI_ACTIVE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    RESOLVED = "RESOLVED"


class SenderType(enum.StrEnum):
    """Who actually authored one message. Stored explicitly (never inferred from role/metadata
    sniffing) so AI can never be mislabeled as human or vice versa."""

    CUSTOMER = "CUSTOMER"
    AI = "AI"
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"


def _enum(cls: type[enum.Enum], name: str, length: int = 20) -> Enum:
    return Enum(cls, native_enum=False, length=length, create_constraint=True, name=name)


class SupportConversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "support_conversations"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_support_conversations_id_org"),
        UniqueConstraint(
            "organization_id",
            "channel",
            "channel_conversation_id",
            name="uq_support_conversations_org_channel_conversation",
        ),
        ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="SET NULL",
            name="fk_support_conversations_customer_org",
        ),
        Index("ix_support_conversations_org_status", "organization_id", "status"),
        Index("ix_support_conversations_org_priority", "organization_id", "priority"),
        Index("ix_support_conversations_org_channel", "organization_id", "channel"),
        Index("ix_support_conversations_org_assigned", "organization_id", "assigned_agent_user_id"),
        Index("ix_support_conversations_org_updated", "organization_id", "updated_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    channel: Mapped[SupportChannel] = mapped_column(_enum(SupportChannel, "support_channel"))
    # Points into conversations.id / whatsapp_conversations.id / email_threads.id depending on
    # `channel`. Deliberately not an FK (polymorphic target); the owning channel's intake code is
    # the only writer and always resolves this id itself, never from untrusted input.
    channel_conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    status: Mapped[SupportStatus] = mapped_column(
        _enum(SupportStatus, "support_status"), default=SupportStatus.OPEN
    )
    priority: Mapped[SupportPriority] = mapped_column(
        _enum(SupportPriority, "support_priority"),
        default=SupportPriority.NORMAL,
        server_default="NORMAL",
    )
    priority_source: Mapped[PrioritySource] = mapped_column(
        _enum(PrioritySource, "support_priority_source"),
        default=PrioritySource.SYSTEM,
        server_default="SYSTEM",
    )
    priority_changed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    priority_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority_reason: Mapped[str | None] = mapped_column(String(200))
    assigned_agent_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    # Authoritative for WEBSITE/EMAIL; mirrored (never independently decided) for WHATSAPP -- see
    # module docstring.
    ai_state: Mapped[AIHandoffState] = mapped_column(
        _enum(AIHandoffState, "support_ai_state"),
        default=AIHandoffState.AI_ACTIVE,
        server_default="AI_ACTIVE",
    )
    handoff_reason: Mapped[str | None] = mapped_column(String(60))
    handoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_customer_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set once, the first time any reply (AI/human/system) goes out -- backs the "average first
    # response time" analytic (brief section 24); never overwritten after.
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_response_sender_type: Mapped[SenderType | None] = mapped_column(
        _enum(SenderType, "support_last_sender_type")
    )
    # Bounded preview only (never the full message), so the inbox list can render without a
    # second query per row. Never trusted for anything but display.
    last_message_preview: Mapped[str | None] = mapped_column(String(300))
    unread: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class SupportTimelineEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only. Backs the customer timeline, the support audit trail, and the consent/
    suppression audit (see module docstring). ``event_type`` is a short fixed vocabulary (e.g.
    ``whatsapp_inbound``, ``email_outbound``, ``ai_reply``, ``human_takeover``,
    ``priority_changed``, ``assignment_changed``, ``consent_changed``, ``email_suppressed``,
    ``campaign_sent``); ``payload`` is bounded, safe-to-display JSON -- never a raw message body,
    token or secret."""

    __tablename__ = "support_timeline_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="CASCADE",
            name="fk_support_timeline_events_customer_org",
        ),
        ForeignKeyConstraint(
            ["support_conversation_id", "organization_id"],
            ["support_conversations.id", "support_conversations.organization_id"],
            ondelete="CASCADE",
            name="fk_support_timeline_events_conversation_org",
        ),
        Index(
            "ix_support_timeline_events_org_customer_created",
            "organization_id",
            "customer_id",
            "created_at",
        ),
        Index(
            "ix_support_timeline_events_org_conversation_created",
            "organization_id",
            "support_conversation_id",
            "created_at",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    support_conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    event_type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
