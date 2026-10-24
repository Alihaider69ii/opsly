"""Keeping webhook subscriptions alive.

Shopify's documentation (https://shopify.dev/docs/apps/build/webhooks/subscribe/https): when a
delivery fails, "Shopify retries 8 times over the next 4 hours" and "after 8 consecutive failures,
the subscription is automatically deleted if it was configured using the Admin API". BizFlow
creates its subscriptions through the Admin API, so an outage of a few hours on our side silently
switches live updates off for every store, and nothing in a normal request would ever notice.

``SubscriptionAuditor`` closes that hole. Run from the worker's maintenance loop it periodically
asks each connected store which subscriptions exist, re-creates missing ones, and, when it finds
that subscriptions we believed were active are gone, queues a catch-up sync, because webhooks were
missed while they were gone. Every step is idempotent and cheap (one GraphQL call per store per
interval); a Shopify failure never raises out of the loop.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select, update

from app.db.models import ShopifyConnection, ShopifyConnectionStatus, WebhookRegistration
from app.services.integrations.shopify import scopes as scope_rules
from app.services.integrations.shopify.auth import SessionFactory
from app.services.integrations.shopify.errors import ShopifyError
from app.services.integrations.shopify.factory import ShopifyRuntime
from app.services.integrations.shopify.lifecycle import webhook_url
from app.services.integrations.shopify.sync import queue_sync
from app.services.integrations.shopify.webhooks import (
    API_TOPICS,
    graphql_topic,
    register_webhooks,
)
from app.workers.queue import JobQueue

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditResult:
    checked: int = 0
    repaired: int = 0
    catch_up_syncs: int = 0


class SubscriptionAuditor:
    """Holds when each connection was last audited (in memory: after a restart everything is
    simply audited again, which costs one call per store)."""

    def __init__(
        self, interval_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._interval = interval_seconds
        self._clock = clock
        self._last: dict[uuid.UUID, float] = {}

    def _due(self, connection_id: uuid.UUID) -> bool:
        last = self._last.get(connection_id)
        return last is None or self._clock() - last >= self._interval

    def run(
        self, rt: ShopifyRuntime, session_factory: SessionFactory, queue: JobQueue
    ) -> AuditResult:
        base = rt.settings.shopify_webhook_base_url
        if not base:
            return AuditResult()  # live updates are not configured at all: nothing to keep alive
        with session_factory() as db:
            candidates = [
                cid
                for cid in db.scalars(
                    select(ShopifyConnection.id).where(
                        ShopifyConnection.status == ShopifyConnectionStatus.CONNECTED
                    )
                )
                if self._due(cid)
            ]
        checked = repaired = catch_up = 0
        for connection_id in candidates:
            self._last[connection_id] = self._clock()  # also after a failure: do not hammer
            try:
                outcome = self._audit_one(
                    rt, session_factory, queue, connection_id, webhook_url(base)
                )
            except ShopifyError as exc:
                logger.warning(
                    "webhook audit failed connection=%s code=%s", connection_id, exc.code
                )
                continue
            checked += 1
            repaired += outcome[0]
            catch_up += outcome[1]
        return AuditResult(checked, repaired, catch_up)

    @staticmethod
    def _audit_one(
        rt: ShopifyRuntime,
        session_factory: SessionFactory,
        queue: JobQueue,
        connection_id: uuid.UUID,
        uri: str,
    ) -> tuple[int, int]:
        with session_factory() as db:
            connection = db.get(ShopifyConnection, connection_id)
            if connection is None or connection.status != ShopifyConnectionStatus.CONNECTED:
                return (0, 0)
            shop, granted = connection.shop_domain, scope_rules.parse_scopes(connection.scopes)
            was = connection.webhook_status

        wanted = {
            graphql_topic(topic)
            for topic in API_TOPICS
            if scope_rules.covers(granted, scope_rules.WEBHOOK_TOPIC_SCOPES[graphql_topic(topic)])
        }
        client = rt.authorized_client(session_factory, connection_id, shop)
        try:
            present = {s["topic"] for s in client.list_webhook_subscriptions() if s["uri"] == uri}
        finally:
            client.close()
        missing = wanted - present
        if not missing:
            if was != WebhookRegistration.REGISTERED:
                with session_factory() as db:
                    db.execute(
                        update(ShopifyConnection)
                        .where(ShopifyConnection.id == connection_id)
                        .values(webhook_status=WebhookRegistration.REGISTERED)
                    )
                    db.commit()
            return (0, 0)

        logger.warning(
            "webhook subscriptions missing connection=%s missing=%d of %d (Shopify removes "
            "subscriptions after 8 consecutive failed deliveries)",
            connection_id,
            len(missing),
            len(wanted),
        )
        register_webhooks(rt, session_factory, connection_id)  # creates only what is missing
        # Webhooks we believed were live were not: whatever changed meanwhile was never delivered.
        believed_live = was in (WebhookRegistration.REGISTERED, WebhookRegistration.PARTIAL)
        started = believed_live and queue_sync(session_factory, queue, connection_id)
        if started:
            logger.info("catch-up sync queued after missing webhooks connection=%s", connection_id)
        return (1, 1 if started else 0)
