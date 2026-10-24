"""Shopify integration tables: the connection, per-resource sync progress, OAuth state and the
webhook ledger. All tenant-owned except where noted; children are tied to their connection's
organization by composite foreign keys, so a row can never point across tenants.

Secrets: the access and refresh tokens are stored only as authenticated ciphertext
(``app.core.secrets``) and are never returned by any API.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
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


class ShopifyConnectionStatus(enum.StrEnum):
    CONNECTED = "CONNECTED"
    SYNCING = "SYNCING"
    ERROR = "ERROR"  # needs the merchant's attention, usually reauthorization
    DISCONNECTED = "DISCONNECTED"


class WebhookRegistration(enum.StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"  # no public webhook URL is configured (normal on localhost)
    REGISTERED = "REGISTERED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class SyncResource(enum.StrEnum):
    PRODUCTS = "PRODUCTS"
    CUSTOMERS = "CUSTOMERS"
    ORDERS = "ORDERS"


class SyncStatus(enum.StrEnum):
    IDLE = "IDLE"
    PENDING = "PENDING"  # queued; the worker claims it atomically
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"  # not permitted (scope or protected-data access), not an error


class WebhookEventStatus(enum.StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    IGNORED = "IGNORED"


def _enum(cls: type[enum.Enum], name: str, length: int = 20) -> Enum:
    return Enum(cls, native_enum=False, length=length, create_constraint=True, name=name)


class ShopifyConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One organization's link to one Shopify store."""

    __tablename__ = "shopify_connections"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_shopify_connections_id_organization_id"),
        # One store per organization for now.
        UniqueConstraint("organization_id", name="uq_shopify_connections_organization_id"),
        # A store can be linked to at most one organization at a time. A DISCONNECTED row has
        # released its claim, so another organization may connect that store later.
        Index(
            "uq_shopify_connections_active_shop",
            "shop_domain",
            unique=True,
            postgresql_where=text("status <> 'DISCONNECTED'"),
            sqlite_where=text("status <> 'DISCONNECTED'"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    shop_domain: Mapped[str] = mapped_column(String(255))
    status: Mapped[ShopifyConnectionStatus] = mapped_column(
        _enum(ShopifyConnectionStatus, "status"),
        default=ShopifyConnectionStatus.CONNECTED,
        index=True,
    )
    # Authenticated ciphertext (bf1:<key id>:...). NULL once disconnected. Never serialized.
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    # Shopify issues expiring offline tokens: a short-lived access token (about an hour) and a
    # rotating refresh token (90 days). Both timestamps drive proactive refresh.
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[str] = mapped_column(String(1000), default="")
    api_version: Mapped[str] = mapped_column(String(20))
    # Normalized shop metadata (no raw Shopify payloads are kept).
    shop_name: Mapped[str | None] = mapped_column(String(255))
    currency: Mapped[str | None] = mapped_column(String(10))
    timezone: Mapped[str | None] = mapped_column(String(64))
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    webhook_status: Mapped[WebhookRegistration] = mapped_column(
        _enum(WebhookRegistration, "webhook_status"), default=WebhookRegistration.NOT_CONFIGURED
    )
    # Safe, user-facing failure information from a fixed catalogue (never raw exception text).
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(String(500))


class ShopifySyncState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Progress of one resource's import. The cursor makes an interrupted import resumable."""

    __tablename__ = "shopify_sync_states"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "resource", name="uq_shopify_sync_states_connection_resource"
        ),
        ForeignKeyConstraint(
            ["connection_id", "organization_id"],
            ["shopify_connections.id", "shopify_connections.organization_id"],
            ondelete="CASCADE",
            name="fk_shopify_sync_states_connection_org",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    resource: Mapped[SyncResource] = mapped_column(_enum(SyncResource, "resource"))
    status: Mapped[SyncStatus] = mapped_column(
        _enum(SyncStatus, "status"), default=SyncStatus.IDLE, index=True
    )
    cursor: Mapped[str | None] = mapped_column(Text)
    items_synced: Mapped[int] = mapped_column(Integer, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, default=0)  # malformed objects, not fatal
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Start of the current full run. Rows not touched since then were not seen in this run.
    run_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(String(500))


class ShopifyOAuthState(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A pending OAuth authorization: single-use, short-lived and bound to the user and
    organization that started it. Only a hash of the ``state`` value is stored."""

    __tablename__ = "shopify_oauth_states"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    shop_domain: Mapped[str] = mapped_column(String(255))
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ShopifyWebhookEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Ledger of received webhooks: the idempotency record, and the work item for the worker.

    Only identifiers are kept (topic, shop, resource id); the webhook body, which can contain
    customer data, is never stored. ``(shop_domain, webhook_id)`` is unique, so a repeated delivery
    is detected by the database itself.
    """

    __tablename__ = "shopify_webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "shop_domain", "webhook_id", name="uq_shopify_webhook_events_shop_webhook"
        ),
        Index("ix_shopify_webhook_events_status_received", "status", "received_at"),
        ForeignKeyConstraint(
            ["connection_id", "organization_id"],
            ["shopify_connections.id", "shopify_connections.organization_id"],
            ondelete="CASCADE",
            name="fk_shopify_webhook_events_connection_org",
        ),
    )

    shop_domain: Mapped[str] = mapped_column(String(255))
    webhook_id: Mapped[str] = mapped_column(String(128))
    event_id: Mapped[str | None] = mapped_column(String(128))
    topic: Mapped[str] = mapped_column(String(100))
    # NULL when the shop is not (or no longer) connected, e.g. shop/redact after an uninstall.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    resource_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[WebhookEventStatus] = mapped_column(
        _enum(WebhookEventStatus, "status"), default=WebhookEventStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(50))
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
