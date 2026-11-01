import enum
import secrets
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
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.support import SenderType  # noqa: F401 (re-exported for schema/API convenience)

PUBLIC_KEY_PREFIX = "pk_"


def generate_public_key() -> str:
    """Unguessable public identifier for the website widget (24 random bytes, URL-safe)."""
    return PUBLIC_KEY_PREFIX + secrets.token_urlsafe(24)


class ConversationChannel(enum.StrEnum):
    WEBSITE = "WEBSITE"


class ConversationStatus(enum.StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class MessageRole(enum.StrEnum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"
    SYSTEM = "SYSTEM"  # reserved (e.g. handoff notes); system prompts are never stored


class ChatWidget(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Per-organization website chat configuration and its PUBLIC identifier.

    The public key is not a secret: it appears in page source. It only selects which organization
    a public request is for; it grants no dashboard access and cannot be used to read other
    organizations. Created lazily the first time an organization opens its widget settings.
    """

    __tablename__ = "chat_widgets"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), unique=True
    )
    public_key: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, default=generate_public_key
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    welcome_message: Mapped[str | None] = mapped_column(String(500))
    # Origins (scheme://host[:port]) allowed to embed the widget. Empty = any site may embed it.
    allowed_origins: Mapped[list[str]] = mapped_column(JSON, default=list)


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One chat thread with one visitor. Tenant-owned."""

    __tablename__ = "conversations"
    __table_args__ = (
        # Target of messages' composite foreign key, which ties messages to the same organization.
        UniqueConstraint("id", "organization_id", name="uq_conversations_id_organization_id"),
        ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="SET NULL",
            name="fk_conversations_customer_org",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[ConversationChannel] = mapped_column(
        Enum(
            ConversationChannel,
            native_enum=False,
            length=20,
            create_constraint=True,
            name="channel",
        ),
        default=ConversationChannel.WEBSITE,
    )
    # Random anonymous id minted by the widget; groups a returning visitor's conversations. It is
    # NOT a credential: access to a conversation is proven by its token (below).
    visitor_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # Phase 9: resolved (read-only, best-effort) once a lead/email/phone identifies a known
    # Customer -- see app/services/support/identity.py. NULL until then; a visitor is never
    # required to identify themselves.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    status: Mapped[ConversationStatus] = mapped_column(
        Enum(
            ConversationStatus, native_enum=False, length=20, create_constraint=True, name="status"
        ),
        default=ConversationStatus.OPEN,
    )
    # SHA-256 of the conversation's secret token, shown to the visitor once at creation.
    token_hash: Mapped[str] = mapped_column(String(64))
    # Number of stored messages; also the position of the newest one.
    message_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Held while a reply is being generated: one in-flight turn per conversation.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "position", name="uq_messages_conversation_position"),
        ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            ["conversations.id", "conversations.organization_id"],
            ondelete="CASCADE",
            name="fk_messages_conversation_org",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    # 1-based order within the conversation; deterministic even when timestamps tie.
    position: Mapped[int] = mapped_column(Integer)
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, native_enum=False, length=20, create_constraint=True, name="role")
    )
    # Plain text. It is never interpreted as HTML anywhere: clients must render it as text.
    content: Mapped[str] = mapped_column(Text)
    # Audit data for assistant turns (grounded?, source pages, model, latency). Never returned to
    # the public API.
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Phase 9: explicit attribution. NULL for rows written before this column existed (none are
    # expected to persist across sessions -- see docs/phase-9-progress.md); every new row sets it.
    sender_type: Mapped[SenderType | None] = mapped_column(
        Enum(SenderType, native_enum=False, length=20, create_constraint=True, name="sender_type")
    )
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
