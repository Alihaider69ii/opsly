"""The dashboard's view of the Shopify integration: status, connect, sync, disconnect.

Every function takes a verified ``OrgContext`` and only ever touches that organization's rows (the
connection is looked up with ``scoped_select``); no organization id is accepted from a client. Roles
are enforced by the router (OWNER/ADMIN for anything that changes state).
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import (
    ShopifyConnection,
    ShopifyConnectionStatus,
    ShopifySyncState,
)
from app.db.scoping import scoped_select
from app.services.integrations.shopify import oauth, store, sync
from app.services.integrations.shopify import scopes as scope_rules
from app.services.integrations.shopify.auth import SessionFactory
from app.services.integrations.shopify.errors import OAuthError
from app.services.integrations.shopify.factory import ShopifyRuntime
from app.services.integrations.shopify.lifecycle import disconnect_store
from app.services.organizations.service import OrgContext
from app.workers.queue import JobQueue

logger = logging.getLogger(__name__)


class NotConnected(Exception):
    pass


class SyncInProgress(Exception):
    pass


class ReauthorizationRequired(Exception):
    pass


class SyncTooSoon(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("sync requested too soon")
        self.retry_after = retry_after


@dataclass(frozen=True)
class StatusView:
    available: bool  # the server has Shopify app credentials
    secure_storage: bool  # the server can encrypt tokens (SECRET_ENCRYPTION_KEY is set)
    webhooks_configured: bool
    connection: ShopifyConnection | None
    sync_states: list[ShopifySyncState]
    counts: store.CommerceCounts
    scopes: list[str]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _connection(db: Session, ctx: OrgContext) -> ShopifyConnection | None:
    return db.scalar(scoped_select(ShopifyConnection, ctx.organization.id))


def get_status(
    db: Session, ctx: OrgContext, settings: Settings, *, secure_storage: bool
) -> StatusView:
    org_id = ctx.organization.id
    connection = _connection(db, ctx)
    states: list[ShopifySyncState] = []
    if connection is not None and connection.status != ShopifyConnectionStatus.DISCONNECTED:
        order = {resource: i for i, resource in enumerate(sync.RESOURCES)}
        states = sorted(
            db.scalars(scoped_select(ShopifySyncState, org_id)).all(),
            key=lambda s: order[s.resource],
        )
    return StatusView(
        available=settings.shopify_configured,
        secure_storage=secure_storage,
        webhooks_configured=bool(settings.shopify_webhook_base_url),
        connection=connection,
        sync_states=states,
        counts=store.commerce_counts(db, org_id),
        scopes=sorted(scope_rules.parse_scopes(connection.scopes)) if connection else [],
    )


def start_connect(
    db: Session, ctx: OrgContext, *, shop: str, settings: Settings, rt: ShopifyRuntime | None
) -> str:
    """The Shopify authorization URL for this organization to visit."""
    if rt is None:
        raise OAuthError("secure storage is not configured", code="secrets_not_configured")
    return oauth.start_authorization(
        db,
        organization_id=ctx.organization.id,
        user_id=ctx.user.id,
        shop_input=shop,
        settings=settings,
        now=_utcnow(),
    )


def finish_connect(
    db: Session,
    ctx: OrgContext,
    *,
    query: Mapping[str, str],
    rt: ShopifyRuntime | None,
    queue: JobQueue,
) -> ShopifyConnection:
    """Complete the OAuth callback for the current user and queue the initial sync (never run
    inside this request: a large store takes a long time)."""
    if rt is None:
        raise OAuthError("secure storage is not configured", code="secrets_not_configured")
    now = _utcnow()
    connection = oauth.complete_authorization(
        db,
        organization_id=ctx.organization.id,
        user_id=ctx.user.id,
        query=query,
        settings=rt.settings,
        secrets_manager=rt.secrets,
        client_for=rt.client_for,
        now=now,
    )
    sync.prepare_sync(db, connection, now)
    db.commit()
    sync.enqueue_sync(db, connection, queue)
    return connection


def request_sync(
    db: Session, ctx: OrgContext, *, settings: Settings, queue: JobQueue
) -> ShopifyConnection:
    org_id = ctx.organization.id
    connection = _connection(db, ctx)
    if connection is None or connection.status == ShopifyConnectionStatus.DISCONNECTED:
        raise NotConnected
    if connection.status == ShopifyConnectionStatus.ERROR:
        raise ReauthorizationRequired
    if connection.status == ShopifyConnectionStatus.SYNCING:
        raise SyncInProgress
    if connection.last_sync_requested_at is not None:
        wait = (
            settings.shopify_sync_min_interval_seconds
            - (_utcnow() - _aware(connection.last_sync_requested_at)).total_seconds()
        )
        if wait > 0:
            raise SyncTooSoon(int(wait) + 1)

    now = _utcnow()
    # Atomic transition: only a connection that is idle can start a sync (two clicks, one sync).
    claimed = db.execute(
        update(ShopifyConnection)
        .where(
            ShopifyConnection.id == connection.id,
            ShopifyConnection.organization_id == org_id,
            ShopifyConnection.status == ShopifyConnectionStatus.CONNECTED,
        )
        .values(status=ShopifyConnectionStatus.SYNCING, last_sync_requested_at=now)
    )
    if claimed.rowcount != 1:  # type: ignore[attr-defined]
        db.rollback()
        raise SyncInProgress
    db.refresh(connection)
    sync.prepare_sync(db, connection, now)
    db.commit()
    sync.enqueue_sync(db, connection, queue)
    return connection


def disconnect(
    db: Session, ctx: OrgContext, *, rt: ShopifyRuntime | None, session_factory: SessionFactory
) -> None:
    """Revoke access, remove webhooks and purge imported data (all re-importable from Shopify)."""
    connection = _connection(db, ctx)
    if connection is None or connection.status == ShopifyConnectionStatus.DISCONNECTED:
        raise NotConnected
    connection_id = connection.id
    db.commit()  # end this request's read transaction; the lifecycle code opens its own
    disconnect_store(rt, session_factory, connection_id, remove_webhooks=True)
