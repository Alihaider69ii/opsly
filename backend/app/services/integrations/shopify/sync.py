"""Importing a store: products, then customers, then orders.

Design (each point has tests):

* **Idempotent.** Every write is an upsert keyed by ``(organization, Shopify id)``; running a sync
  twice, or two workers overlapping, never duplicates a row.
* **Resumable.** Progress lives in ``shopify_sync_states``. Each page is written and its cursor
  saved in the *same* transaction, so an interrupted import continues from the last committed page.
* **Time-sliced.** A job works for at most ``SHOPIFY_SYNC_SLICE_SECONDS``, saves its place and
  re-queues itself, so a large import cannot starve crawls and webhooks on the same worker.
* **Ordered.** Products are imported before customers and orders, so order line items link to their
  products and customers.
* **Bounded memory.** One page (20 to 50 objects) at a time; nothing accumulates across pages.
* **Tolerant.** A malformed object is counted and skipped. A resource Shopify will not let us read
  (missing scope, protected customer data) is SKIPPED, not a failure of the whole sync.
* **Observable.** Per-resource status, counts, cursor and a safe error code are visible in the
  dashboard; logs carry identifiers and counts, never customer data.
* **Safe to claim.** A resource is claimed with one atomic UPDATE (PENDING -> RUNNING), so two
  workers can never run the same stage; a worker that dies is detected by its missing heartbeat.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import (
    ShopifyConnection,
    ShopifyConnectionStatus,
    ShopifySyncState,
    SyncResource,
    SyncStatus,
)
from app.services.integrations.shopify import fetch, store
from app.services.integrations.shopify import scopes as scope_rules
from app.services.integrations.shopify.auth import SessionFactory, mark_reauthorization_required
from app.services.integrations.shopify.client import ShopifyClient
from app.services.integrations.shopify.errors import (
    USER_MESSAGES,
    ShopifyAccessDenied,
    ShopifyAuthError,
    ShopifyError,
    ShopifyThrottled,
    ShopifyUnavailable,
    user_message,
)
from app.services.integrations.shopify.factory import ShopifyRuntime
from app.services.integrations.shopify.webhooks import register_webhooks
from app.workers.queue import JobQueue, ShopifySyncJob

logger = logging.getLogger(__name__)

RESOURCES = (SyncResource.PRODUCTS, SyncResource.CUSTOMERS, SyncResource.ORDERS)
RESOURCE_SCOPES = {
    SyncResource.PRODUCTS: scope_rules.READ_PRODUCTS,
    SyncResource.CUSTOMERS: scope_rules.READ_CUSTOMERS,
    SyncResource.ORDERS: scope_rules.READ_ORDERS,
}
MAX_ATTEMPTS = 3  # job-level attempts for transient failures, on top of the client's own retries
ACTIVE = (SyncStatus.PENDING, SyncStatus.RUNNING)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# --- starting a sync -----------------------------------------------------------------------------


def prepare_sync(db: Session, connection: ShopifyConnection, now: datetime) -> None:
    """Reset every resource's progress for a fresh full import (the caller commits and enqueues).

    Resources whose scope was not granted are marked SKIPPED up front: that is a decision the
    merchant made, not a failure.
    """
    granted = scope_rules.parse_scopes(connection.scopes)
    existing = {
        s.resource: s
        for s in db.scalars(
            select(ShopifySyncState).where(ShopifySyncState.connection_id == connection.id)
        )
    }
    for resource in RESOURCES:
        state = existing.get(resource)
        if state is None:
            state = ShopifySyncState(
                organization_id=connection.organization_id,
                connection_id=connection.id,
                resource=resource,
            )
            db.add(state)
        permitted = scope_rules.covers(granted, RESOURCE_SCOPES[resource])
        state.status = SyncStatus.PENDING if permitted else SyncStatus.SKIPPED
        state.cursor = None
        state.items_synced = 0
        state.items_skipped = 0
        state.attempts = 0
        state.run_started_at = None
        state.started_at = None
        state.finished_at = None if permitted else now
        state.heartbeat_at = None
        state.error_code = None if permitted else "scope_missing"
        state.error_message = None if permitted else user_message("scope_missing")
    connection.status = ShopifyConnectionStatus.SYNCING
    connection.error_code = None
    connection.error_message = None
    connection.last_sync_requested_at = now


def enqueue_sync(db: Session, connection: ShopifyConnection, queue: JobQueue) -> None:
    """Queue the import; if the queue is down, fail the stages visibly instead of leaving them
    PENDING forever (and leave the connection usable)."""
    try:
        queue.enqueue(ShopifySyncJob(connection.id))
    except Exception:
        logger.exception("could not enqueue shopify sync connection=%s", connection.id)
        db.execute(
            update(ShopifySyncState)
            .where(
                ShopifySyncState.connection_id == connection.id,
                ShopifySyncState.status == SyncStatus.PENDING,
            )
            .values(
                status=SyncStatus.FAILED,
                finished_at=_utcnow(),
                error_code="queue_unavailable",
                error_message=user_message("queue_unavailable"),
            )
        )
        connection.status = ShopifyConnectionStatus.CONNECTED
        db.commit()


def queue_sync(session_factory: SessionFactory, queue: JobQueue, connection_id: uuid.UUID) -> bool:
    """Start a sync on the system's own initiative (not a merchant's click). It uses the same
    atomic CONNECTED -> SYNCING flip, so it never overlaps another sync. False if not possible."""
    now = _utcnow()
    with session_factory() as db:
        claimed = db.execute(
            update(ShopifyConnection)
            .where(
                ShopifyConnection.id == connection_id,
                ShopifyConnection.status == ShopifyConnectionStatus.CONNECTED,
            )
            .values(status=ShopifyConnectionStatus.SYNCING, last_sync_requested_at=now)
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            db.rollback()
            return False
        connection = db.get(ShopifyConnection, connection_id)
        assert connection is not None
        db.refresh(connection)
        prepare_sync(db, connection, now)
        db.commit()
        enqueue_sync(db, connection, queue)
    return True


# --- one job -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Claimed:
    state_id: uuid.UUID
    resource: SyncResource
    cursor: str | None
    run_started_at: datetime | None
    attempts: int


def _claim_next(session_factory: SessionFactory, connection_id: uuid.UUID) -> _Claimed | None:
    """Claim the earliest unfinished resource, but only if everything before it is finished."""
    with session_factory() as db:
        states = {
            s.resource: s
            for s in db.scalars(
                select(ShopifySyncState).where(ShopifySyncState.connection_id == connection_id)
            )
        }
        for resource in RESOURCES:
            state = states.get(resource)
            if state is None or state.status not in ACTIVE:
                continue
            if state.status == SyncStatus.RUNNING:
                return None  # another worker is on the earliest unfinished stage
            now = _utcnow()
            result = db.execute(
                update(ShopifySyncState)
                .where(
                    ShopifySyncState.id == state.id, ShopifySyncState.status == SyncStatus.PENDING
                )
                .values(
                    status=SyncStatus.RUNNING,
                    heartbeat_at=now,
                    started_at=func.coalesce(ShopifySyncState.started_at, now),
                    run_started_at=func.coalesce(ShopifySyncState.run_started_at, now),
                    attempts=ShopifySyncState.attempts + 1,
                    error_code=None,
                    error_message=None,
                )
            )
            db.commit()
            if result.rowcount != 1:  # type: ignore[attr-defined]
                return None
            db.refresh(state)
            return _Claimed(
                state.id, state.resource, state.cursor, _aware(state.run_started_at), state.attempts
            )
    return None


def _set_state(session_factory: SessionFactory, state_id: uuid.UUID, **values: Any) -> None:
    with session_factory() as db:
        db.execute(update(ShopifySyncState).where(ShopifySyncState.id == state_id).values(**values))
        db.commit()


def _heartbeat(session_factory: SessionFactory, state_id: uuid.UUID) -> bool:
    """Record liveness. False means the stage was cancelled (e.g. the store was disconnected)."""
    with session_factory() as db:
        result = db.execute(
            update(ShopifySyncState)
            .where(ShopifySyncState.id == state_id, ShopifySyncState.status == SyncStatus.RUNNING)
            .values(heartbeat_at=_utcnow())
        )
        db.commit()
        return result.rowcount == 1  # type: ignore[attr-defined]


def _write_page(
    db: Session,
    resource: SyncResource,
    org_id: uuid.UUID,
    items: list[Any],
    currency: str | None,
    now: datetime,
) -> None:
    if resource == SyncResource.PRODUCTS:
        store.upsert_products(db, org_id, items, currency=currency, now=now)
    elif resource == SyncResource.CUSTOMERS:
        store.upsert_customers(db, org_id, items, now=now)
    else:
        store.upsert_orders(db, org_id, items, now=now)


_FETCHERS: dict[SyncResource, Callable[[ShopifyClient, str | None], fetch.Page[Any]]] = {
    SyncResource.PRODUCTS: fetch.fetch_products_page,
    SyncResource.CUSTOMERS: fetch.fetch_customers_page,
    SyncResource.ORDERS: fetch.fetch_orders_page,
}


def _run_resource(
    rt: ShopifyRuntime,
    session_factory: SessionFactory,
    client: ShopifyClient,
    claimed: _Claimed,
    *,
    org_id: uuid.UUID,
    currency: str | None,
    deadline: float,
    should_stop: Callable[[], bool],
) -> str:
    """Import pages until done. Returns done | slice | stopped | cancelled (errors propagate)."""
    fetch_page = _FETCHERS[claimed.resource]
    cursor = claimed.cursor
    restricted = False
    while True:
        if should_stop():
            return "stopped"
        if not _heartbeat(session_factory, claimed.state_id):
            return "cancelled"
        page = fetch_page(client, cursor)
        restricted = restricted or page.restricted
        now = _utcnow()
        with session_factory() as db:
            # Lock the stage row for the length of this write. Disconnecting deletes it, so a
            # disconnect either waits for this page and then purges it, or commits first and
            # this sees no row and writes nothing. Without the lock a page could commit
            # after the purge and leave a disconnected shop's customers behind.
            status = db.scalar(
                select(ShopifySyncState.status)
                .where(ShopifySyncState.id == claimed.state_id)
                .with_for_update()
            )
            if status != SyncStatus.RUNNING:
                return "cancelled"
            _write_page(db, claimed.resource, org_id, page.items, currency, now)
            db.execute(
                update(ShopifySyncState)
                .where(ShopifySyncState.id == claimed.state_id)
                .values(
                    items_synced=ShopifySyncState.items_synced + len(page.items),
                    items_skipped=ShopifySyncState.items_skipped + page.malformed,
                    cursor=page.next_cursor if page.has_next else None,
                    heartbeat_at=now,
                )
            )
            db.commit()  # the page and the cursor that points past it commit together
        cursor = page.next_cursor
        if not page.has_next:
            break
        if rt.monotonic() >= deadline:
            return "slice"

    with session_factory() as db:
        if claimed.resource != SyncResource.ORDERS and claimed.run_started_at is not None:
            store.soft_delete_unseen(
                db,
                org_id,
                products=claimed.resource == SyncResource.PRODUCTS,
                customers=claimed.resource == SyncResource.CUSTOMERS,
                since=claimed.run_started_at,
            )
        db.execute(
            update(ShopifySyncState)
            .where(ShopifySyncState.id == claimed.state_id)
            .values(
                status=SyncStatus.COMPLETED,
                finished_at=_utcnow(),
                cursor=None,
                # A warning, not a failure: Shopify withheld some fields (protected customer data).
                error_code="access_denied" if restricted else None,
                error_message=user_message("access_denied") if restricted else None,
            )
        )
        db.commit()
    return "done"


def _fail_remaining(session_factory: SessionFactory, connection_id: uuid.UUID, code: str) -> None:
    with session_factory() as db:
        db.execute(
            update(ShopifySyncState)
            .where(
                ShopifySyncState.connection_id == connection_id,
                ShopifySyncState.status.in_(ACTIVE),
            )
            .values(
                status=SyncStatus.FAILED,
                finished_at=_utcnow(),
                error_code=code,
                error_message=user_message(code),
            )
        )
        db.commit()


def _finalize(session_factory: SessionFactory, connection_id: uuid.UUID) -> str:
    with session_factory() as db:
        states = list(
            db.scalars(
                select(ShopifySyncState).where(ShopifySyncState.connection_id == connection_id)
            )
        )
        if any(s.status in ACTIVE for s in states):
            return "pending"  # stages are waiting (a retry pause) or running elsewhere
        connection = db.get(ShopifyConnection, connection_id)
        failed = any(s.status == SyncStatus.FAILED for s in states)
        if connection is not None and connection.status == ShopifyConnectionStatus.SYNCING:
            connection.status = ShopifyConnectionStatus.CONNECTED
            if not failed:
                connection.last_sync_at = _utcnow()
        db.commit()
    return "failed" if failed else "completed"


def run_sync_job(
    rt: ShopifyRuntime,
    session_factory: SessionFactory,
    connection_id: uuid.UUID,
    *,
    queue: JobQueue,
    should_stop: Callable[[], bool] = lambda: False,
) -> str:
    """Run (or continue) one connection's import for at most one time slice.

    Returns skipped, continued, interrupted, cancelled, retry_later, pending, completed or failed.
    """
    deadline = rt.monotonic() + rt.settings.shopify_sync_slice_seconds
    with session_factory() as db:
        connection = db.get(ShopifyConnection, connection_id)
        if connection is None or connection.status != ShopifyConnectionStatus.SYNCING:
            return "skipped"
        org_id, shop, currency = (
            connection.organization_id,
            connection.shop_domain,
            connection.currency,
        )

    logger.info("shopify sync started connection=%s org=%s shop=%s", connection_id, org_id, shop)
    client = rt.authorized_client(session_factory, connection_id, shop)
    try:
        while True:
            claimed = _claim_next(session_factory, connection_id)
            if claimed is None:
                break
            if (
                claimed.resource == SyncResource.PRODUCTS
                and claimed.attempts == 1
                and claimed.cursor is None
            ):
                register_webhooks(rt, session_factory, connection_id)  # best effort; never raises
            try:
                result = _run_resource(
                    rt,
                    session_factory,
                    client,
                    claimed,
                    org_id=org_id,
                    currency=currency,
                    deadline=deadline,
                    should_stop=should_stop,
                )
            except ShopifyAuthError as exc:
                code = exc.code if exc.code in USER_MESSAGES else "auth_failed"
                _set_state(
                    session_factory,
                    claimed.state_id,
                    status=SyncStatus.FAILED,
                    finished_at=_utcnow(),
                    error_code=code,
                    error_message=user_message(code),
                )
                _fail_remaining(session_factory, connection_id, code)
                with session_factory() as db:
                    row = db.get(ShopifyConnection, connection_id)
                    if row is not None and row.status != ShopifyConnectionStatus.DISCONNECTED:
                        mark_reauthorization_required(row, code)
                        db.commit()
                logger.warning(
                    "shopify sync stopped: authorization connection=%s code=%s", connection_id, code
                )
                return "failed"
            except ShopifyAccessDenied:
                _set_state(
                    session_factory,
                    claimed.state_id,
                    status=SyncStatus.FAILED
                    if claimed.resource == SyncResource.PRODUCTS
                    else SyncStatus.SKIPPED,
                    finished_at=_utcnow(),
                    error_code="access_denied",
                    error_message=user_message("access_denied"),
                )
                continue
            except (ShopifyThrottled, ShopifyUnavailable) as exc:
                if claimed.attempts >= MAX_ATTEMPTS:
                    _set_state(
                        session_factory,
                        claimed.state_id,
                        status=SyncStatus.FAILED,
                        finished_at=_utcnow(),
                        error_code=exc.code,
                        error_message=user_message(exc.code),
                    )
                    continue
                # Keep the cursor and wait: the worker's maintenance loop re-queues a PENDING stage
                # after a pause, which is the delayed retry.
                _set_state(
                    session_factory,
                    claimed.state_id,
                    status=SyncStatus.PENDING,
                    error_code=exc.code,
                    error_message=user_message(exc.code),
                )
                return "retry_later"
            except ShopifyError as exc:
                _set_state(
                    session_factory,
                    claimed.state_id,
                    status=SyncStatus.FAILED,
                    finished_at=_utcnow(),
                    error_code=exc.code if exc.code in USER_MESSAGES else "request_failed",
                    error_message=user_message(
                        exc.code if exc.code in USER_MESSAGES else "request_failed"
                    ),
                )
                continue
            except Exception:
                logger.exception(
                    "shopify sync crashed connection=%s resource=%s",
                    connection_id,
                    claimed.resource,
                )
                _set_state(
                    session_factory,
                    claimed.state_id,
                    status=SyncStatus.FAILED,
                    finished_at=_utcnow(),
                    error_code="internal_error",
                    error_message=user_message("internal_error"),
                )
                continue

            if result in ("slice", "stopped"):
                _set_state(session_factory, claimed.state_id, status=SyncStatus.PENDING)
                queue.enqueue(ShopifySyncJob(connection_id))
                return "continued" if result == "slice" else "interrupted"
            if result == "cancelled":
                return "cancelled"
    finally:
        client.close()

    outcome = _finalize(session_factory, connection_id)
    logger.info("shopify sync finished connection=%s outcome=%s", connection_id, outcome)
    return outcome


# --- maintenance ---------------------------------------------------------------------------------


def reap_stale_sync_states(
    session_factory: SessionFactory, queue: JobQueue, stale_after_seconds: int
) -> int:
    """A RUNNING stage whose worker stopped heart-beating goes back to PENDING (its cursor is
    intact, so the import resumes) unless it has already been tried too often, then it FAILs."""
    cutoff = _utcnow() - timedelta(seconds=stale_after_seconds)
    resumed: set[uuid.UUID] = set()
    with session_factory() as db:
        stale = list(
            db.scalars(
                select(ShopifySyncState).where(
                    ShopifySyncState.status == SyncStatus.RUNNING,
                    ShopifySyncState.heartbeat_at < cutoff,
                )
            )
        )
        for state in stale:
            if state.attempts >= MAX_ATTEMPTS:
                state.status = SyncStatus.FAILED
                state.finished_at = _utcnow()
                state.error_code = "interrupted"
                state.error_message = user_message("interrupted")
            else:
                state.status = SyncStatus.PENDING
                resumed.add(state.connection_id)
        db.commit()
    for connection_id in resumed:
        queue.enqueue(ShopifySyncJob(connection_id))
    if stale:
        logger.warning("recovered %d stale shopify sync stage(s)", len(stale))
    return len(stale)


def requeue_orphaned_sync(
    session_factory: SessionFactory, queue: JobQueue, older_than_seconds: int
) -> int:
    """Re-enqueue PENDING stages that have waited a while (a retry pause, or a job lost with
    Redis). Claiming is atomic, so a duplicate job is harmless. Connections that are no longer
    syncing, and stages already finished, are not touched."""
    cutoff = _utcnow() - timedelta(seconds=older_than_seconds)
    with session_factory() as db:
        connection_ids = set(
            db.scalars(
                select(ShopifySyncState.connection_id)
                .join(
                    ShopifyConnection,
                    ShopifyConnection.id == ShopifySyncState.connection_id,
                )
                .where(
                    ShopifySyncState.status == SyncStatus.PENDING,
                    ShopifySyncState.updated_at < cutoff,
                    ShopifyConnection.status == ShopifyConnectionStatus.SYNCING,
                )
            )
        )
    for connection_id in connection_ids:
        queue.enqueue(ShopifySyncJob(connection_id))
    return len(connection_ids)
