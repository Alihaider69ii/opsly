"""Ending a connection: revoke access, stop work, remove imported data.

Used by the dashboard's "Disconnect", by Shopify's ``app/uninstalled`` webhook and by the
``shop/redact`` compliance webhook, so all three behave the same way.

Order matters. First the credentials are wiped and the connection is marked DISCONNECTED and its
sync progress removed, in one transaction: from that instant no worker can use the store and every
in-flight sync notices (its next heartbeat fails). Only then is the imported data purged, in
batches. If the purge is interrupted, ``finish_pending_purges`` completes it from the worker's
maintenance loop, so personal data is never left behind.

Everything imported is re-importable from Shopify, which is why disconnecting removes it rather
than keeping stale customer data and a catalogue that Phase 5 could mistake for current.
"""

import logging
import uuid
from datetime import timedelta

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import (
    Customer,
    Order,
    Product,
    ShopifyConnection,
    ShopifyConnectionStatus,
    ShopifySyncState,
    WebhookRegistration,
)
from app.services.integrations.shopify import store
from app.services.integrations.shopify.auth import SessionFactory, clear_tokens
from app.services.integrations.shopify.errors import ShopifyError
from app.services.integrations.shopify.factory import ShopifyRuntime

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/api/webhooks/shopify"


def webhook_url(base_url: str) -> str:
    return base_url.rstrip("/") + WEBHOOK_PATH


def _remove_webhook_subscriptions(
    rt: ShopifyRuntime,
    session_factory: SessionFactory,
    connection_id: uuid.UUID,
    shop_domain: str,
) -> int:
    """Best effort: delete this app's webhook subscriptions. Never raises."""
    base = rt.settings.shopify_webhook_base_url
    if not base:
        return 0
    uri = webhook_url(base)
    removed = 0
    client = rt.authorized_client(session_factory, connection_id, shop_domain)
    try:
        for subscription in client.list_webhook_subscriptions():
            if subscription["uri"] == uri and client.delete_webhook_subscription(
                subscription["id"]
            ):
                removed += 1
    except ShopifyError as exc:
        logger.info(
            "could not remove shopify webhooks connection=%s code=%s", connection_id, exc.code
        )
    finally:
        client.close()
    return removed


def disconnect_store(
    rt: ShopifyRuntime | None,
    session_factory: SessionFactory,
    connection_id: uuid.UUID,
    *,
    remove_webhooks: bool,
    purge: bool = True,
) -> bool:
    """Disconnect and (by default) purge. Returns False if the connection did not exist."""
    with session_factory() as db:
        connection = db.get(ShopifyConnection, connection_id)
        if connection is None:
            return False
        organization_id, shop_domain = connection.organization_id, connection.shop_domain
        was_connected = connection.status != ShopifyConnectionStatus.DISCONNECTED

    if remove_webhooks and was_connected and rt is not None:
        _remove_webhook_subscriptions(rt, session_factory, connection_id, shop_domain)

    with session_factory() as db:
        connection = db.get(ShopifyConnection, connection_id)
        if connection is None:
            return False
        connection.status = ShopifyConnectionStatus.DISCONNECTED
        clear_tokens(connection)
        connection.error_code = None
        connection.error_message = None
        connection.webhook_status = WebhookRegistration.NOT_CONFIGURED
        db.execute(delete(ShopifySyncState).where(ShopifySyncState.connection_id == connection_id))
        db.commit()
    logger.info("shopify disconnected org=%s shop=%s", organization_id, shop_domain)

    if purge:
        with session_factory() as db:
            removed = store.purge_shop_data(db, organization_id)
        logger.info("shopify data purged org=%s removed=%s", organization_id, removed)
    return True


def rotate_aging_tokens(
    rt: ShopifyRuntime, session_factory: SessionFactory, *, within: timedelta = timedelta(days=30)
) -> int:
    """Rotate refresh tokens that are close to expiring. A refresh token lives 90 days from its
    last use; a store with no traffic for that long would otherwise lose its connection silently."""
    cutoff = store.utcnow() + within
    with session_factory() as db:
        ids = list(
            db.scalars(
                select(ShopifyConnection.id).where(
                    ShopifyConnection.status.in_(
                        [ShopifyConnectionStatus.CONNECTED, ShopifyConnectionStatus.SYNCING]
                    ),
                    ShopifyConnection.refresh_token_encrypted.is_not(None),
                    ShopifyConnection.refresh_token_expires_at < cutoff,
                )
            )
        )
    rotated = 0
    for connection_id in ids:
        try:
            rt.tokens_for(session_factory, connection_id).refresh(force=True)
            rotated += 1
        except ShopifyError as exc:
            logger.warning("token rotation failed connection=%s code=%s", connection_id, exc.code)
    return rotated


def finish_pending_purges(session_factory: SessionFactory) -> int:
    """Complete the purge of every disconnected store that still has imported rows (a purge
    may have been interrupted, or a page may have been written by a worker that was already
    past its last check). There is deliberately no age limit: personal data must not outlive
    a disconnect because a worker happened to be down for a while. One indexed EXISTS query
    finds the organizations; nothing else runs when there is none."""
    with session_factory() as db:
        has_rows = or_(
            *(
                exists().where(model.organization_id == ShopifyConnection.organization_id)
                for model in (Product, Customer, Order)
            )
        )
        org_ids = list(
            db.scalars(
                select(ShopifyConnection.organization_id).where(
                    ShopifyConnection.status == ShopifyConnectionStatus.DISCONNECTED,
                    has_rows,
                )
            )
        )
    done = 0
    for organization_id in org_ids:
        try:
            with session_factory() as db:
                store.purge_shop_data(db, organization_id)
            done += 1
        except SQLAlchemyError:
            logger.exception("purge retry failed org=%s", organization_id)
    return done
