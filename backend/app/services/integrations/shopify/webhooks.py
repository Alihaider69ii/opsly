"""Shopify webhooks: registration, intake and processing.

**Intake** (the HTTP request, which must answer within Shopify's 5 seconds) does no real work:

    verify HMAC -> validate shop/topic/id -> deduplicate -> store a ledger row -> enqueue -> 200

**Processing** happens in the worker. A webhook is treated as a *notification*, not as data: the
worker re-fetches the current object from Shopify by id and upserts it. That makes three hard
problems disappear: a replayed or out-of-order delivery cannot roll data back (the fetch returns
the truth), a forged-but-signed stale payload has nothing to inject, and no webhook body (which may
contain customer data) ever has to be stored. Only identifiers are kept in the ledger.

Duplicates: Shopify may deliver the same webhook more than once and identifies a delivery with
``X-Shopify-Webhook-Id``. ``(shop, webhook id)`` is unique in the database, so a repeat is detected
by the database itself and acknowledged without a second job.
"""

import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import (
    ShopifyConnection,
    ShopifyConnectionStatus,
    ShopifyWebhookEvent,
    WebhookEventStatus,
    WebhookRegistration,
)
from app.services.integrations.shopify import fetch, store
from app.services.integrations.shopify import scopes as scope_rules
from app.services.integrations.shopify.auth import SessionFactory
from app.services.integrations.shopify.errors import (
    InvalidShopDomain,
    ShopifyAccessDenied,
    ShopifyAuthError,
    ShopifyError,
    ShopifyThrottled,
    ShopifyUnavailable,
)
from app.services.integrations.shopify.factory import ShopifyRuntime
from app.services.integrations.shopify.lifecycle import disconnect_store, webhook_url
from app.services.integrations.shopify.validation import (
    normalize_shop_domain,
    to_int_id,
    verify_webhook_hmac,
)
from app.workers.queue import JobQueue, ShopifyWebhookJob

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5

# Topics registered through the API (header form, lower case); the GraphQL enum is the upper-case,
# underscore form. Which scope each needs lives in ``scopes.WEBHOOK_TOPIC_SCOPES``.
API_TOPICS = tuple(t.lower().replace("_", "/", 1) for t in scope_rules.WEBHOOK_TOPIC_SCOPES)
# Mandatory privacy topics: configured in the app's settings in Shopify, not through the API.
COMPLIANCE_TOPICS = ("customers/data_request", "customers/redact", "shop/redact")
HANDLED_TOPICS = frozenset(API_TOPICS) | frozenset(COMPLIANCE_TOPICS)
_NEEDS_ID = {
    "products/create",
    "products/update",
    "products/delete",
    "customers/create",
    "customers/update",
    "customers/delete",
    "orders/create",
    "orders/updated",
    "orders/cancelled",
}


def graphql_topic(topic: str) -> str:
    """``orders/updated`` -> ``ORDERS_UPDATED``."""
    return topic.upper().replace("/", "_")


class WebhookRejected(Exception):
    """The webhook is refused. ``status_code`` is what to answer; ``reason`` is for the log."""

    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


@dataclass(frozen=True)
class IntakeResult:
    status: Literal["accepted", "duplicate", "ignored"]
    event_id: uuid.UUID | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- registration --------------------------------------------------------------------------------


def register_webhooks(
    rt: ShopifyRuntime, session_factory: SessionFactory, connection_id: uuid.UUID
) -> WebhookRegistration:
    """Subscribe to every needed topic the granted scopes allow. Idempotent: existing
    subscriptions to our URL are kept, only missing ones are created. Never raises: webhooks are an
    optimisation (a manual sync always works), so a refusal is recorded and shown, not fatal."""
    base = rt.settings.shopify_webhook_base_url
    with session_factory() as db:
        connection = db.get(ShopifyConnection, connection_id)
        if connection is None or connection.status == ShopifyConnectionStatus.DISCONNECTED:
            return WebhookRegistration.NOT_CONFIGURED
        shop_domain, granted = connection.shop_domain, scope_rules.parse_scopes(connection.scopes)

    result = WebhookRegistration.NOT_CONFIGURED
    if base:
        uri = webhook_url(base)
        wanted = [
            graphql_topic(topic)
            for topic in API_TOPICS
            if scope_rules.covers(granted, scope_rules.WEBHOOK_TOPIC_SCOPES[graphql_topic(topic)])
        ]
        client = rt.authorized_client(session_factory, connection_id, shop_domain)
        try:
            existing = {(s["topic"], s["uri"]) for s in client.list_webhook_subscriptions()}
            created = failed = 0
            for topic in wanted:
                if (topic, uri) in existing:
                    created += 1
                elif client.create_webhook_subscription(topic, uri):
                    created += 1
                else:
                    failed += 1
            if wanted and failed == 0:
                result = WebhookRegistration.REGISTERED
            elif created:
                result = WebhookRegistration.PARTIAL
            elif wanted:
                result = WebhookRegistration.FAILED
        except ShopifyError as exc:
            logger.warning(
                "webhook registration failed connection=%s code=%s", connection_id, exc.code
            )
            result = WebhookRegistration.FAILED
        finally:
            client.close()

    with session_factory() as db:
        db.execute(
            update(ShopifyConnection)
            .where(ShopifyConnection.id == connection_id)
            .values(webhook_status=result)
        )
        db.commit()
    return result


# --- intake --------------------------------------------------------------------------------------


def _resource_id(topic: str, payload: Mapping[str, Any]) -> int | None:
    if topic in ("customers/redact", "customers/data_request"):
        customer = payload.get("customer")
        return to_int_id(customer.get("id")) if isinstance(customer, Mapping) else None
    if topic in _NEEDS_ID:
        return to_int_id(payload.get("id"))
    return None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def receive_webhook(
    db: Session,
    *,
    body: bytes,
    headers: Mapping[str, str],
    settings: Settings,
    queue: JobQueue,
    now: datetime | None = None,
) -> IntakeResult:
    """Handle one delivery. ``headers`` must have lower-cased names. Raises ``WebhookRejected``."""
    now = now or _utcnow()
    secret = (
        settings.shopify_client_secret.get_secret_value() if settings.shopify_client_secret else ""
    )
    if not verify_webhook_hmac(body, headers.get("x-shopify-hmac-sha256"), secret):
        raise WebhookRejected(401, "invalid signature")

    topic = (headers.get("x-shopify-topic") or "").strip().lower()
    webhook_id = (headers.get("x-shopify-webhook-id") or "").strip()
    try:
        shop = normalize_shop_domain(headers.get("x-shopify-shop-domain") or "")
    except InvalidShopDomain:
        raise WebhookRejected(400, "invalid shop domain") from None
    if not webhook_id or len(webhook_id) > 128:
        raise WebhookRejected(400, "missing webhook id")
    if topic not in HANDLED_TOPICS:
        return IntakeResult("ignored")  # a topic we never subscribed to: acknowledge, do nothing
    try:
        payload = json.loads(body)
    except ValueError:
        raise WebhookRejected(400, "body is not JSON") from None
    if not isinstance(payload, dict):
        raise WebhookRejected(400, "body is not an object")
    resource_id = _resource_id(topic, payload)
    if topic in _NEEDS_ID and resource_id is None:
        raise WebhookRejected(400, "payload has no valid id")

    connection = db.scalar(
        select(ShopifyConnection).where(
            ShopifyConnection.shop_domain == shop,
            ShopifyConnection.status != ShopifyConnectionStatus.DISCONNECTED,
        )
    )
    if connection is None and topic in COMPLIANCE_TOPICS:
        # shop/redact arrives after an uninstall, when the connection is already DISCONNECTED.
        connection = db.scalar(
            select(ShopifyConnection)
            .where(ShopifyConnection.shop_domain == shop)
            .order_by(ShopifyConnection.updated_at.desc())
        )
    if connection is None:
        return IntakeResult("ignored")  # not (or no longer) our store

    if db.scalar(
        select(ShopifyWebhookEvent.id).where(
            ShopifyWebhookEvent.shop_domain == shop, ShopifyWebhookEvent.webhook_id == webhook_id
        )
    ):
        return IntakeResult("duplicate")
    event = ShopifyWebhookEvent(
        id=uuid.uuid4(),
        shop_domain=shop,
        webhook_id=webhook_id,
        event_id=(headers.get("x-shopify-event-id") or "")[:128] or None,
        topic=topic,
        organization_id=connection.organization_id,
        connection_id=connection.id,
        resource_id=resource_id,
        status=WebhookEventStatus.PENDING,
        triggered_at=_parse_time(headers.get("x-shopify-triggered-at")),
        received_at=now,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # two deliveries raced: the database's unique constraint picked one
        return IntakeResult("duplicate")
    try:
        queue.enqueue(ShopifyWebhookJob(event.id))
    except Exception:
        logger.exception("could not enqueue webhook event=%s", event.id)
        db.execute(delete(ShopifyWebhookEvent).where(ShopifyWebhookEvent.id == event.id))
        db.commit()  # forget it, so Shopify's retry is not mistaken for a duplicate
        raise WebhookRejected(503, "queue unavailable") from None
    logger.info("shopify webhook accepted shop=%s topic=%s event=%s", shop, topic, event.id)
    return IntakeResult("accepted", event.id)


# --- processing ----------------------------------------------------------------------------------


def _claim(session_factory: SessionFactory, event_id: uuid.UUID) -> ShopifyWebhookEvent | None:
    with session_factory() as db:
        result = db.execute(
            update(ShopifyWebhookEvent)
            .where(
                ShopifyWebhookEvent.id == event_id,
                ShopifyWebhookEvent.status == WebhookEventStatus.PENDING,
            )
            .values(status=WebhookEventStatus.PROCESSING, attempts=ShopifyWebhookEvent.attempts + 1)
        )
        db.commit()
        if result.rowcount != 1:  # type: ignore[attr-defined]
            return None
        event = db.get(ShopifyWebhookEvent, event_id)
        if event is not None:
            db.expunge(event)
        return event


def _finish(
    session_factory: SessionFactory,
    event_id: uuid.UUID,
    status: WebhookEventStatus,
    error_code: str | None = None,
) -> None:
    with session_factory() as db:
        db.execute(
            update(ShopifyWebhookEvent)
            .where(ShopifyWebhookEvent.id == event_id)
            .values(
                status=status,
                error_code=error_code,
                processed_at=_utcnow() if status != WebhookEventStatus.PENDING else None,
            )
        )
        db.commit()


def _apply(
    rt: ShopifyRuntime,
    session_factory: SessionFactory,
    event: ShopifyWebhookEvent,
    connection_id: uuid.UUID,
    organization_id: uuid.UUID,
    shop_domain: str,
    currency: str | None,
) -> None:
    """Make the local data match Shopify for one resource."""
    topic, resource_id = event.topic, event.resource_id
    now = _utcnow()
    if resource_id is None:
        return
    resource, _, action = topic.partition("/")
    if action == "delete":
        with session_factory() as db:
            if resource == "products":
                store.mark_product_deleted(db, organization_id, resource_id, now)
            else:
                store.mark_customer_deleted(db, organization_id, resource_id, now)
            db.commit()
        return

    client = rt.authorized_client(session_factory, connection_id, shop_domain)
    try:
        if resource == "products":
            product = fetch.fetch_product(client, resource_id)
        elif resource == "customers":
            customer = fetch.fetch_customer(client, resource_id)
        else:
            order = fetch.fetch_order(client, resource_id)
    finally:
        client.close()

    with session_factory() as db:
        if resource == "products":
            if product is None:  # gone by the time we asked
                store.mark_product_deleted(db, organization_id, resource_id, now)
            else:
                store.upsert_products(db, organization_id, [product], currency=currency, now=now)
        elif resource == "customers":
            if customer is None:
                store.mark_customer_deleted(db, organization_id, resource_id, now)
            else:
                store.upsert_customers(db, organization_id, [customer], now=now)
        elif order is not None:
            store.upsert_orders(db, organization_id, [order], now=now)
        db.commit()


def process_event(rt: ShopifyRuntime, session_factory: SessionFactory, event_id: uuid.UUID) -> str:
    """Run one stored webhook event. Returns ``skipped`` (not claimable), ``processed``,
    ``ignored``, ``retry`` (transient failure, retried later) or ``failed``."""
    event = _claim(session_factory, event_id)
    if event is None:
        return "skipped"
    with session_factory() as db:
        connection = db.get(ShopifyConnection, event.connection_id) if event.connection_id else None
        info = (
            None
            if connection is None
            else (
                connection.id,
                connection.organization_id,
                connection.shop_domain,
                connection.currency,
                connection.status,
            )
        )
    if info is None:
        _finish(session_factory, event_id, WebhookEventStatus.IGNORED, "unknown_store")
        return "ignored"
    connection_id, organization_id, shop_domain, currency, status = info

    try:
        topic = event.topic
        if topic == "app/uninstalled":
            disconnect_store(rt, session_factory, connection_id, remove_webhooks=False)
        elif topic == "shop/redact":
            if status != ShopifyConnectionStatus.DISCONNECTED:
                # The merchant reinstalled before the redact arrived: this is current data.
                _finish(session_factory, event_id, WebhookEventStatus.IGNORED, "reinstalled")
                return "ignored"
            disconnect_store(rt, session_factory, connection_id, remove_webhooks=False)
        elif topic == "customers/redact":
            if event.resource_id is not None:
                with session_factory() as db:
                    store.anonymize_customer(db, organization_id, event.resource_id, _utcnow())
                    db.commit()
        elif topic == "customers/data_request":
            # BizFlow stores no customer data beyond what Shopify holds; a data-subject export is
            # fulfilled by the merchant from Shopify. Recorded for the audit trail only.
            logger.info("shopify customer data request received org=%s", organization_id)
        elif status == ShopifyConnectionStatus.DISCONNECTED:
            _finish(session_factory, event_id, WebhookEventStatus.IGNORED, "not_connected")
            return "ignored"
        else:
            _apply(
                rt, session_factory, event, connection_id, organization_id, shop_domain, currency
            )
    except (ShopifyThrottled, ShopifyUnavailable) as exc:
        if event.attempts >= MAX_ATTEMPTS:
            _finish(session_factory, event_id, WebhookEventStatus.FAILED, exc.code)
            return "failed"
        _finish(session_factory, event_id, WebhookEventStatus.PENDING, exc.code)
        return "retry"  # the worker's maintenance loop re-queues it after a pause
    except ShopifyAuthError as exc:
        _finish(session_factory, event_id, WebhookEventStatus.FAILED, exc.code)
        return "failed"
    except ShopifyAccessDenied:
        _finish(session_factory, event_id, WebhookEventStatus.FAILED, "access_denied")
        return "failed"
    except ShopifyError as exc:
        _finish(session_factory, event_id, WebhookEventStatus.FAILED, exc.code)
        return "failed"
    except Exception:
        logger.exception("webhook processing crashed event=%s", event_id)
        _finish(session_factory, event_id, WebhookEventStatus.FAILED, "internal_error")
        return "failed"
    _finish(session_factory, event_id, WebhookEventStatus.PROCESSED)
    return "processed"


# --- maintenance ---------------------------------------------------------------------------------


def requeue_pending_events(
    session_factory: SessionFactory, queue: JobQueue, older_than_seconds: int
) -> int:
    """Re-enqueue PENDING events that have waited too long (a transient failure, or a job lost with
    Redis) and recover events stuck PROCESSING from a crashed worker. Claiming is atomic, so a
    duplicate job is harmless."""
    cutoff = _utcnow() - timedelta(seconds=older_than_seconds)
    with session_factory() as db:
        db.execute(
            update(ShopifyWebhookEvent)
            .where(
                ShopifyWebhookEvent.status == WebhookEventStatus.PROCESSING,
                ShopifyWebhookEvent.received_at < cutoff,
            )
            .values(status=WebhookEventStatus.PENDING)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        ids = list(
            db.scalars(
                select(ShopifyWebhookEvent.id).where(
                    ShopifyWebhookEvent.status == WebhookEventStatus.PENDING,
                    ShopifyWebhookEvent.received_at < cutoff,
                )
            )
        )
    for event_id in ids:
        queue.enqueue(ShopifyWebhookJob(event_id))
    return len(ids)


def prune_old_events(session_factory: SessionFactory, retention_days: int) -> int:
    """Delete finished ledger rows older than the retention window. Deduplication only needs to
    outlive Shopify's retry window (hours), so 30 days is generous."""
    cutoff = _utcnow() - timedelta(days=retention_days)
    with session_factory() as db:
        result = db.execute(
            delete(ShopifyWebhookEvent)
            .where(
                ShopifyWebhookEvent.received_at < cutoff,
                ShopifyWebhookEvent.status.in_(
                    [
                        WebhookEventStatus.PROCESSED,
                        WebhookEventStatus.IGNORED,
                        WebhookEventStatus.FAILED,
                    ]
                ),
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
