import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from app.db.models import (
    Customer,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    ShopifyConnection,
    ShopifyConnectionStatus,
    ShopifySyncState,
    SyncResource,
    SyncStatus,
    WebhookRegistration,
)
from app.services.integrations.shopify import lifecycle, store, sync
from app.workers.queue import InMemoryJobQueue, ShopifySyncJob
from tests.helpers import create_org, signup_and_login
from tests.shopify.conftest import seed_connection
from tests.shopify.fake_shopify import (
    make_customer,
    make_line_item,
    make_order,
    make_product,
    make_variant,
)


@pytest.fixture()
def org_id(client):
    return uuid.UUID(
        create_org(client, signup_and_login(client, "sync@example.com"), "Sync Co")["id"]
    )


@pytest.fixture()
def cid(session_factory, org_id, runtime, fake):
    return seed_connection(session_factory, org_id, runtime, fake)


@pytest.fixture()
def jobs() -> InMemoryJobQueue:
    return InMemoryJobQueue()


def begin(session_factory, cid, runtime):
    with session_factory() as db:
        sync.prepare_sync(db, db.get(ShopifyConnection, cid), runtime.now())
        db.commit()


def run(runtime, session_factory, cid, jobs, **kw):
    return sync.run_sync_job(runtime, session_factory, cid, queue=jobs, **kw)


def full_sync(runtime, session_factory, cid, jobs, rounds=20):
    """Prepare and run slices until the sync stops asking to continue."""
    begin(session_factory, cid, runtime)
    outcome = ""
    for _ in range(rounds):
        outcome = run(runtime, session_factory, cid, jobs)
        if outcome != "continued":
            return outcome
        jobs.jobs.clear()
    raise AssertionError("sync did not finish")


def states(session_factory, cid) -> dict[str, ShopifySyncState]:
    with session_factory() as db:
        return {
            s.resource.value: s
            for s in db.scalars(
                select(ShopifySyncState).where(ShopifySyncState.connection_id == cid)
            )
        }


def connection(session_factory, cid) -> ShopifyConnection:
    with session_factory() as db:
        return db.get(ShopifyConnection, cid)


def n(db, model, **where) -> int:
    stmt = select(func.count()).select_from(model)
    for k, v in where.items():
        stmt = stmt.where(getattr(model, k) == v)
    return int(db.scalar(stmt))


def populate(fake, products=45, customers=120, orders=45):
    fake.products = [
        make_product(i, variants=[make_variant(i * 100 + 1), make_variant(i * 100 + 2)])
        for i in range(1, products + 1)
    ]
    fake.customers = [make_customer(i) for i in range(1, customers + 1)]
    fake.orders = [
        make_order(
            i,
            customer=(i % customers) + 1,
            items=[
                make_line_item(
                    i * 10 + 1, product=(i % products) + 1, variant=((i % products) + 1) * 100 + 1
                )
            ],
        )
        for i in range(1, orders + 1)
    ]


# --- the happy path --------------------------------------------------------------------------------------


def test_a_full_sync_imports_everything_across_pages(
    runtime, session_factory, cid, fake, jobs, db, org_id
):
    populate(fake)
    assert full_sync(runtime, session_factory, cid, jobs) == "completed"
    assert (
        n(db, Product),
        n(db, ProductVariant),
        n(db, Customer),
        n(db, Order),
        n(db, OrderItem),
    ) == (45, 90, 120, 45, 45)
    assert {
        k: (s.status, s.items_synced, s.items_skipped, s.cursor)
        for k, s in states(session_factory, cid).items()
    } == {
        "PRODUCTS": (SyncStatus.COMPLETED, 45, 0, None),
        "CUSTOMERS": (SyncStatus.COMPLETED, 120, 0, None),
        "ORDERS": (SyncStatus.COMPLETED, 45, 0, None),
    }
    c = connection(session_factory, cid)
    assert c.status == ShopifyConnectionStatus.CONNECTED and c.last_sync_at is not None
    ops = [o for o in fake.operations() if o in ("Products", "Customers", "Orders")]
    assert (
        ops == ["Products"] * 3 + ["Customers"] * 3 + ["Orders"] * 3
    )  # ceil(45/20), ceil(120/50), ceil(45/20)


def test_pagination_follows_cursors_without_repeating_or_skipping(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=45, customers=0, orders=0)
    full_sync(runtime, session_factory, cid, jobs)
    afters = [r["variables"]["after"] for r in fake.requests if r["operation"] == "Products"]
    assert afters == [None, "19", "39"]
    assert sorted(db.scalars(select(Product.shopify_product_id)).all()) == list(range(1, 46))


def test_an_empty_store_syncs_cleanly(runtime, session_factory, cid, fake, jobs, db):
    assert full_sync(runtime, session_factory, cid, jobs) == "completed"
    assert n(db, Product) == 0
    assert all(
        s.status == SyncStatus.COMPLETED and s.items_synced == 0
        for s in states(session_factory, cid).values()
    )


def test_running_the_sync_again_never_duplicates_data(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=25, customers=60, orders=25)
    for _ in range(3):
        assert full_sync(runtime, session_factory, cid, jobs) == "completed"
    assert (
        n(db, Product),
        n(db, ProductVariant),
        n(db, Customer),
        n(db, Order),
        n(db, OrderItem),
    ) == (25, 50, 60, 25, 25)


def test_changes_in_shopify_are_picked_up_on_the_next_sync(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=3, customers=2, orders=1)
    full_sync(runtime, session_factory, cid, jobs)
    fake.products[0]["title"] = "Renamed"
    fake.products[0]["updatedAt"] = "2026-10-09T10:00:00Z"
    fake.products[0]["_variants"] = [
        make_variant(101, price="99.00", updated="2026-10-09T10:00:00Z")
    ]
    full_sync(runtime, session_factory, cid, jobs)
    db.expire_all()
    assert db.scalar(select(Product.title).where(Product.shopify_product_id == 1)) == "Renamed"
    assert n(db, ProductVariant, shopify_variant_id=101) == 1
    assert (
        n(db, ProductVariant, shopify_variant_id=102, deleted_at=None) == 0
    )  # the removed variant is gone


def test_order_items_are_linked_to_products_customers_and_variants(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=3, customers=3, orders=3)
    full_sync(runtime, session_factory, cid, jobs)
    item = db.scalars(select(OrderItem).order_by(OrderItem.shopify_line_item_id)).first()
    assert (
        item.product_id is not None and item.variant_id is not None
    )  # products were imported first
    assert db.scalar(select(Order.customer_id).where(Order.shopify_order_id == 1)) is not None


# --- nested connections ------------------------------------------------------------------------------------


def test_products_with_many_variants_are_completed_with_follow_up_pages(
    runtime, session_factory, cid, fake, jobs, db
):
    fake.products = [
        make_product(1, variants=[make_variant(1000 + i) for i in range(60)]),
        make_product(2),
    ]
    full_sync(runtime, session_factory, cid, jobs)
    assert n(db, ProductVariant) == 61
    assert "ProductVariants" in fake.operations()
    full_sync(runtime, session_factory, cid, jobs)  # and re-syncing keeps all 60, deleting none
    assert n(db, ProductVariant, deleted_at=None) == 61


def test_orders_with_many_line_items_are_completed_with_follow_up_pages(
    runtime, session_factory, cid, fake, jobs, db
):
    fake.orders = [
        make_order(1, items=[make_line_item(i, product=None, variant=None) for i in range(1, 71)])
    ]
    full_sync(runtime, session_factory, cid, jobs)
    assert n(db, OrderItem) == 70 and "OrderLineItems" in fake.operations()


# --- tolerance ---------------------------------------------------------------------------------------------


def test_malformed_objects_are_skipped_and_counted_without_failing_the_sync(
    runtime, session_factory, cid, fake, jobs, db
):
    fake.products = [make_product(1), {"id": "junk", "title": "no valid id"}, make_product(3)]
    fake.customers = [make_customer(1), {"nonsense": True}]
    fake.orders = [make_order(1), {"id": "gid://shopify/Product/9"}]
    assert full_sync(runtime, session_factory, cid, jobs) == "completed"
    assert (n(db, Product), n(db, Customer), n(db, Order)) == (2, 1, 1)
    st = states(session_factory, cid)
    assert (
        st["PRODUCTS"].items_skipped,
        st["CUSTOMERS"].items_skipped,
        st["ORDERS"].items_skipped,
    ) == (1, 1, 1)
    assert all(s.status == SyncStatus.COMPLETED for s in st.values())


def test_an_unexpected_exception_fails_that_resource_with_a_safe_message_and_the_rest_continue(
    runtime, session_factory, cid, fake, jobs, db, monkeypatch, caplog
):
    populate(fake, products=2, customers=2, orders=1)

    def explode(client, cursor):
        raise RuntimeError("secret internal detail /srv/app/trace")

    monkeypatch.setitem(sync._FETCHERS, SyncResource.CUSTOMERS, explode)
    assert full_sync(runtime, session_factory, cid, jobs) == "failed"
    st = states(session_factory, cid)
    assert (
        st["PRODUCTS"].status == SyncStatus.COMPLETED
        and st["ORDERS"].status == SyncStatus.COMPLETED
    )
    assert (
        st["CUSTOMERS"].status == SyncStatus.FAILED
        and st["CUSTOMERS"].error_code == "internal_error"
    )
    assert "secret internal detail" not in (
        st["CUSTOMERS"].error_message or ""
    )  # only the catalogue text
    assert connection(session_factory, cid).status == ShopifyConnectionStatus.CONNECTED
    assert (
        connection(session_factory, cid).last_sync_at is None
    )  # a sync with a failure is not "synced"


# --- permissions ---------------------------------------------------------------------------------------------


def test_a_resource_shopify_will_not_show_is_skipped_not_a_failure(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=2, customers=2, orders=2)
    fake.deny = {"customers"}
    assert full_sync(runtime, session_factory, cid, jobs) == "completed"
    st = states(session_factory, cid)
    assert (st["CUSTOMERS"].status, st["CUSTOMERS"].error_code) == (
        SyncStatus.SKIPPED,
        "access_denied",
    )
    assert "protected customer data" in st["CUSTOMERS"].error_message
    assert st["PRODUCTS"].status == st["ORDERS"].status == SyncStatus.COMPLETED
    assert (n(db, Product), n(db, Customer), n(db, Order)) == (2, 0, 2)
    assert connection(session_factory, cid).last_sync_at is not None


def test_customers_whose_personal_data_is_withheld_are_still_imported(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=1, customers=3, orders=1)
    fake.restrict_customer_pii = True
    assert full_sync(runtime, session_factory, cid, jobs) == "completed"
    assert n(db, Customer) == 3
    assert all(c.email is None and c.first_name is None for c in db.scalars(select(Customer)))
    st = states(session_factory, cid)["CUSTOMERS"]
    assert (st.status, st.error_code) == (
        SyncStatus.COMPLETED,
        "access_denied",
    )  # a warning on a finished stage


def test_being_denied_the_catalog_fails_that_stage(runtime, session_factory, cid, fake, jobs):
    fake.deny = {"products"}
    assert full_sync(runtime, session_factory, cid, jobs) == "failed"
    assert states(session_factory, cid)["PRODUCTS"].status == SyncStatus.FAILED


def test_declined_optional_scopes_mean_those_calls_are_never_made(
    runtime, session_factory, org_id, fake, jobs
):
    cid = seed_connection(session_factory, org_id, runtime, fake, scopes="read_products")
    populate(fake, products=2, customers=5, orders=5)
    assert full_sync(runtime, session_factory, cid, jobs) == "completed"
    st = states(session_factory, cid)
    assert (st["CUSTOMERS"].status, st["ORDERS"].status) == (SyncStatus.SKIPPED, SyncStatus.SKIPPED)
    assert "Customers" not in fake.operations() and "Orders" not in fake.operations()


# --- rate limits and retries ----------------------------------------------------------------------------------


def test_throttling_during_a_sync_is_absorbed_with_backoff(
    runtime, session_factory, cid, fake, jobs, db, clock
):
    populate(fake, products=2, customers=2, orders=1)
    fake.throttle_responses = 3
    assert full_sync(runtime, session_factory, cid, jobs) == "completed"
    assert n(db, Product) == 2 and clock.sleeps  # it waited rather than hammering Shopify


def test_a_nearly_empty_bucket_slows_the_import_down(
    runtime, session_factory, cid, fake, jobs, clock
):
    populate(fake, products=45, customers=0, orders=0)
    fake.bucket_available = 80.0
    full_sync(runtime, session_factory, cid, jobs)
    assert sum(clock.sleeps) > 5  # paced by the reported bucket, not a fixed delay


def test_persistent_throttling_pauses_the_stage_for_a_later_retry_instead_of_failing_or_spinning(
    runtime, session_factory, cid, fake, jobs
):
    populate(fake, products=2)
    fake.throttle_responses = 10_000
    begin(session_factory, cid, runtime)
    assert run(runtime, session_factory, cid, jobs) == "retry_later"
    st = states(session_factory, cid)
    assert st["PRODUCTS"].status == SyncStatus.PENDING and st["PRODUCTS"].error_code == "throttled"
    assert st["PRODUCTS"].attempts == 1
    assert st["CUSTOMERS"].status == SyncStatus.PENDING  # nothing after it was attempted
    assert connection(session_factory, cid).status == ShopifyConnectionStatus.SYNCING
    assert jobs.jobs == type(jobs.jobs)()  # no immediate hot-loop re-queue


def test_a_paused_stage_is_retried_by_maintenance_and_finishes_when_shopify_recovers(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=2, customers=1, orders=1)
    fake.throttle_responses = 10_000
    begin(session_factory, cid, runtime)
    run(runtime, session_factory, cid, jobs)
    fake.throttle_responses = 0
    assert sync.requeue_orphaned_sync(session_factory, jobs, 300) == 0  # too soon: not orphaned yet
    with session_factory() as s:
        s.execute(
            update(ShopifySyncState).values(updated_at=datetime.now(UTC) - timedelta(minutes=10))
        )
        s.commit()
    assert sync.requeue_orphaned_sync(session_factory, jobs, 300) == 1
    assert isinstance(jobs.jobs.popleft(), ShopifySyncJob)
    assert run(runtime, session_factory, cid, jobs) == "completed"
    assert n(db, Product) == 2


def test_after_enough_attempts_a_stage_fails_and_the_rest_still_run(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=2, customers=1, orders=1)
    begin(session_factory, cid, runtime)
    fake.http_failures = [503] * 10_000
    for _ in range(sync.MAX_ATTEMPTS):
        outcome = run(runtime, session_factory, cid, jobs)
        if outcome != "retry_later":
            break
    assert states(session_factory, cid)["PRODUCTS"].status == SyncStatus.FAILED
    assert states(session_factory, cid)["PRODUCTS"].error_code == "unavailable"


def test_a_permanent_request_error_is_not_retried(
    runtime, session_factory, cid, fake, jobs, shopify_settings
):
    shopify_settings.shopify_webhook_base_url = ""  # so the first call is the product page
    populate(fake, products=2, customers=0, orders=0)
    fake.http_failures = [400]
    begin(session_factory, cid, runtime)
    run(runtime, session_factory, cid, jobs)
    st = states(session_factory, cid)["PRODUCTS"]
    assert (st.status, st.error_code, st.attempts) == (SyncStatus.FAILED, "request_failed", 1)
    assert fake.operations().count("Products") == 1


# --- authorization ---------------------------------------------------------------------------------------------


def test_a_revoked_connection_stops_the_sync_and_asks_for_reauthorization(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=2, customers=2, orders=2)
    full_sync(runtime, session_factory, cid, jobs)
    fake.revoke_all_tokens()  # the merchant uninstalled the app
    with session_factory() as s:
        s.execute(update(ShopifyConnection).values(access_token_expires_at=None))
        s.commit()
    assert full_sync(runtime, session_factory, cid, jobs) == "failed"
    c = connection(session_factory, cid)
    assert c.status == ShopifyConnectionStatus.ERROR and c.error_code in (
        "reauthorization_required",
        "auth_failed",
    )
    st = states(session_factory, cid)
    assert all(s.status == SyncStatus.FAILED for s in st.values())
    assert n(db, Product) == 2  # previously imported data is not thrown away by a failed refresh


def test_tokens_expiring_mid_sync_are_refreshed_transparently(
    runtime, session_factory, cid, fake, jobs, db, clock
):
    populate(fake, products=45, customers=0, orders=0)
    inner = fake.handler
    seen = {"n": 0}

    def ticking(request):
        seen["n"] += 1
        if seen["n"] == 3:
            clock.advance(3550)  # the hour passes between pages
        return inner(request)

    runtime.transport = httpx.MockTransport(ticking)
    assert full_sync(runtime, session_factory, cid, jobs) == "completed"
    assert n(db, Product) == 45
    assert [r["grant_type"] for r in fake.token_requests if "grant_type" in r] == ["refresh_token"]


# --- resumability and time slicing -------------------------------------------------------------------------------


def test_an_interrupted_sync_resumes_from_its_saved_cursor(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=45, customers=0, orders=0)
    begin(session_factory, cid, runtime)
    pages = {"n": 0}

    def stop_after_one_page():
        pages["n"] += 1
        return pages["n"] > 1  # the worker is asked to shut down before the second page

    assert (
        run(runtime, session_factory, cid, jobs, should_stop=stop_after_one_page) == "interrupted"
    )
    st = states(session_factory, cid)["PRODUCTS"]
    assert st.status == SyncStatus.PENDING and st.cursor == "19" and st.items_synced == 20
    assert isinstance(jobs.jobs.popleft(), ShopifySyncJob)  # handed back to the queue

    before = fake.operations().count("Products")
    assert run(runtime, session_factory, cid, jobs) == "completed"
    afters = [r["variables"]["after"] for r in fake.requests if r["operation"] == "Products"]
    assert afters == [None, "19", "39"]  # page 1 was never fetched again
    assert fake.operations().count("Products") - before == 2
    assert n(db, Product) == 45
    assert states(session_factory, cid)["PRODUCTS"].items_synced == 45


def test_a_long_import_is_cut_into_time_slices_that_requeue_themselves(
    runtime, session_factory, cid, fake, jobs, db, clock, shopify_settings
):
    populate(fake, products=100, customers=0, orders=0)  # 5 pages
    shopify_settings.shopify_sync_slice_seconds = 15

    def slow(request):
        clock.advance(10)  # every request takes ten "seconds"
        return fake.handler(request)

    runtime.transport = httpx.MockTransport(slow)
    begin(session_factory, cid, runtime)
    outcomes = []
    for _ in range(10):
        outcomes.append(run(runtime, session_factory, cid, jobs))
        if outcomes[-1] != "continued":
            break
        assert isinstance(jobs.jobs.popleft(), ShopifySyncJob)
    assert outcomes[:-1] == ["continued"] * (len(outcomes) - 1) and len(outcomes) > 2
    assert outcomes[-1] == "completed"
    assert n(db, Product) == 100


def test_webhooks_are_registered_once_per_run_not_on_every_slice(
    runtime, session_factory, cid, fake, jobs, clock, shopify_settings
):
    populate(fake, products=100, customers=0, orders=0)
    shopify_settings.shopify_sync_slice_seconds = 15
    runtime.transport = httpx.MockTransport(lambda r: (clock.advance(10), fake.handler(r))[1])
    full_sync(runtime, session_factory, cid, jobs)
    assert fake.operations().count("WebhookSubscriptions") == 1


# --- webhook registration ---------------------------------------------------------------------------------------


def test_a_sync_registers_the_webhook_subscriptions(runtime, session_factory, cid, fake, jobs):
    full_sync(runtime, session_factory, cid, jobs)
    topics = sorted(s["topic"] for s in fake.subscriptions)
    assert topics == sorted(
        [
            "PRODUCTS_CREATE",
            "PRODUCTS_UPDATE",
            "PRODUCTS_DELETE",
            "CUSTOMERS_CREATE",
            "CUSTOMERS_UPDATE",
            "CUSTOMERS_DELETE",
            "ORDERS_CREATE",
            "ORDERS_UPDATED",
            "ORDERS_CANCELLED",
            "APP_UNINSTALLED",
        ]
    )
    assert {s["uri"] for s in fake.subscriptions} == {
        "https://api.example.com/api/webhooks/shopify"
    }
    assert connection(session_factory, cid).webhook_status == WebhookRegistration.REGISTERED
    full_sync(runtime, session_factory, cid, jobs)  # idempotent
    assert len(fake.subscriptions) == 10


def test_without_a_public_url_webhooks_are_not_attempted(
    runtime, session_factory, cid, fake, jobs, shopify_settings
):
    shopify_settings.shopify_webhook_base_url = ""
    full_sync(runtime, session_factory, cid, jobs)
    assert fake.subscriptions == [] and "WebhookSubscriptions" not in fake.operations()
    assert connection(session_factory, cid).webhook_status == WebhookRegistration.NOT_CONFIGURED


def test_only_topics_the_granted_scopes_allow_are_subscribed(
    runtime, session_factory, org_id, fake, jobs
):
    cid = seed_connection(session_factory, org_id, runtime, fake, scopes="read_products")
    full_sync(runtime, session_factory, cid, jobs)
    assert sorted(s["topic"] for s in fake.subscriptions) == [
        "APP_UNINSTALLED",
        "PRODUCTS_CREATE",
        "PRODUCTS_DELETE",
        "PRODUCTS_UPDATE",
    ]


def test_a_refused_subscription_is_recorded_but_never_breaks_the_sync(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=2, customers=0, orders=0)
    fake.refuse_webhook_topics = {"ORDERS_CREATE"}
    assert full_sync(runtime, session_factory, cid, jobs) == "completed"
    assert connection(session_factory, cid).webhook_status == WebhookRegistration.PARTIAL
    assert n(db, Product) == 2


# --- cancellation, reconciliation, recovery ------------------------------------------------------------------------


def test_disconnecting_mid_sync_stops_it_and_leaves_no_data(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=45, customers=0, orders=0)
    inner = fake.handler
    state = {"products": 0}

    def disconnect_after_first_page(request):
        response = inner(request)
        if b"query Products" in request.content:
            state["products"] += 1
            if state["products"] == 1:
                lifecycle.disconnect_store(runtime, session_factory, cid, remove_webhooks=False)
        return response

    runtime.transport = httpx.MockTransport(disconnect_after_first_page)
    begin(session_factory, cid, runtime)
    assert run(runtime, session_factory, cid, jobs) == "cancelled"
    assert fake.operations().count("Products") == 1  # it noticed and stopped fetching
    assert n(db, Product) == 0
    assert connection(session_factory, cid).status == ShopifyConnectionStatus.DISCONNECTED


def test_rows_missing_from_a_complete_import_are_marked_deleted_but_orders_never_are(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=3, customers=3, orders=3)
    full_sync(runtime, session_factory, cid, jobs)
    fake.products = fake.products[:2]
    fake.customers = fake.customers[:2]
    fake.orders = fake.orders[:1]
    full_sync(runtime, session_factory, cid, jobs)
    db.expire_all()
    assert (n(db, Product, deleted_at=None), n(db, Product)) == (
        2,
        3,
    )  # the third is kept, inactive
    assert (n(db, Customer, deleted_at=None), n(db, Customer)) == (2, 3)
    assert n(db, Order) == 3  # order history is preserved


def test_reconciliation_does_not_run_when_the_import_did_not_complete(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=45, customers=0, orders=0)
    full_sync(runtime, session_factory, cid, jobs)
    fake.products = fake.products[:5]
    begin(session_factory, cid, runtime)
    assert run(runtime, session_factory, cid, jobs, should_stop=lambda: True) == "interrupted"
    assert (
        n(db, Product, deleted_at=None) == 45
    )  # nothing was marked deleted by a half-finished run


def test_only_one_worker_can_claim_a_stage_and_stages_run_in_order(runtime, session_factory, cid):
    begin(session_factory, cid, runtime)
    first = sync._claim_next(session_factory, cid)
    assert first is not None and first.resource == SyncResource.PRODUCTS and first.attempts == 1
    assert sync._claim_next(session_factory, cid) is None  # the earliest unfinished stage is taken


def test_a_job_for_a_connection_that_is_not_syncing_is_ignored(
    runtime, session_factory, cid, jobs, fake
):
    assert run(runtime, session_factory, cid, jobs) == "skipped"
    assert run(runtime, session_factory, uuid.uuid4(), jobs) == "skipped"
    assert fake.requests == []


def test_a_duplicate_job_after_completion_does_nothing(runtime, session_factory, cid, fake, jobs):
    populate(fake, products=2, customers=0, orders=0)
    full_sync(runtime, session_factory, cid, jobs)
    calls = len(fake.requests)
    assert run(runtime, session_factory, cid, jobs) == "skipped"
    assert len(fake.requests) == calls


def test_a_stage_whose_worker_died_is_resumed_not_lost(
    runtime, session_factory, cid, fake, jobs, db
):
    populate(fake, products=45, customers=0, orders=0)
    begin(session_factory, cid, runtime)
    claimed = sync._claim_next(session_factory, cid)  # a worker claimed it, then was killed
    with session_factory() as s:
        s.execute(
            update(ShopifySyncState)
            .where(ShopifySyncState.id == claimed.state_id)
            .values(
                heartbeat_at=datetime.now(UTC) - timedelta(hours=1), cursor="19", items_synced=20
            )
        )
        s.commit()
    assert sync.reap_stale_sync_states(session_factory, jobs, 600) == 1
    assert states(session_factory, cid)["PRODUCTS"].status == SyncStatus.PENDING
    assert isinstance(jobs.jobs.popleft(), ShopifySyncJob)
    assert run(runtime, session_factory, cid, jobs) == "completed"
    afters = [r["variables"]["after"] for r in fake.requests if r["operation"] == "Products"]
    assert afters == ["19", "39"]  # resumed after the last saved page


def test_the_reaper_leaves_healthy_stages_alone_and_gives_up_on_repeatedly_dying_ones(
    runtime, session_factory, cid, jobs
):
    begin(session_factory, cid, runtime)
    claimed = sync._claim_next(session_factory, cid)
    assert sync.reap_stale_sync_states(session_factory, jobs, 600) == 0  # heartbeat is fresh
    with session_factory() as s:
        s.execute(
            update(ShopifySyncState)
            .where(ShopifySyncState.id == claimed.state_id)
            .values(heartbeat_at=datetime.now(UTC) - timedelta(hours=1), attempts=sync.MAX_ATTEMPTS)
        )
        s.commit()
    assert sync.reap_stale_sync_states(session_factory, jobs, 600) == 1
    st = states(session_factory, cid)["PRODUCTS"]
    assert (st.status, st.error_code) == (SyncStatus.FAILED, "interrupted")
    assert len(jobs.jobs) == 0


def test_orphaned_pending_work_is_requeued_only_for_connections_that_are_syncing(
    runtime, session_factory, cid, jobs
):
    begin(session_factory, cid, runtime)
    old = datetime.now(UTC) - timedelta(hours=1)
    with session_factory() as s:
        s.execute(update(ShopifySyncState).values(updated_at=old))
        s.commit()
    assert (
        sync.requeue_orphaned_sync(session_factory, jobs, 300) == 1
    )  # one job per connection, not per stage
    with session_factory() as s:
        s.execute(update(ShopifyConnection).values(status=ShopifyConnectionStatus.CONNECTED))
        s.commit()
    assert sync.requeue_orphaned_sync(session_factory, jobs, 300) == 0


def test_sync_progress_survives_in_the_database_so_the_dashboard_can_show_it(
    runtime, session_factory, cid, fake, jobs
):
    populate(fake, products=45, customers=0, orders=0)
    begin(session_factory, cid, runtime)
    pages = {"n": 0}
    run(
        runtime,
        session_factory,
        cid,
        jobs,
        should_stop=lambda: pages.__setitem__("n", pages["n"] + 1) or pages["n"] > 2,
    )
    st = states(session_factory, cid)["PRODUCTS"]
    assert st.items_synced == 40 and st.started_at is not None and st.heartbeat_at is not None
    assert st.finished_at is None


def test_counts_reflect_what_was_imported(runtime, session_factory, cid, fake, jobs, db, org_id):
    populate(fake, products=4, customers=3, orders=2)
    full_sync(runtime, session_factory, cid, jobs)
    c = store.commerce_counts(db, org_id)
    assert (c.products, c.variants, c.customers, c.orders) == (4, 8, 3, 2)
