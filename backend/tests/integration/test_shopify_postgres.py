"""Phase 4 on real infrastructure: the Shopify schema and its constraints, tenant isolation enforced
by the database itself, upserts under concurrency, webhook idempotency through the real unique
constraint, the sync state machine, token-refresh locking, and the API on Postgres + Redis.

The unit tier proves the logic against SQLite; this tier proves the parts that only a real database
can: composite foreign keys, partial unique indexes, ``ON CONFLICT``, ``SELECT ... FOR UPDATE`` and
atomic claims between concurrent workers.
"""

import base64
import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.core.secrets import SecretManager
from app.db.database import get_session_factory
from app.db.models import (
    Customer,
    Order,
    OrderItem,
    Organization,
    Product,
    ProductVariant,
    ShopifyConnection,
    ShopifyConnectionStatus,
    ShopifySyncState,
    ShopifyWebhookEvent,
    SyncResource,
    SyncStatus,
    WebhookEventStatus,
)
from app.main import app
from app.services.commerce import queries
from app.services.integrations.shopify import mapper, store, sync, webhooks
from app.services.integrations.shopify.factory import ShopifyRuntime
from app.services.integrations.shopify.validation import sign_webhook_body
from app.workers.queue import (
    CrawlJob,
    RedisJobQueue,
    ShopifySyncJob,
    ShopifyWebhookJob,
    parse_job,
)
from tests.integration.conftest import TEST_DATABASE_URL, TEST_REDIS_URL
from tests.shopify.conftest import (
    Clock,
    callback_query,
    seed_connection,
    state_from,
)
from tests.shopify.fake_shopify import (
    FakeShopify,
    make_customer,
    make_line_item,
    make_order,
    make_product,
    make_variant,
)

BASE = "/api/integrations/shopify"
HOOK = "/api/webhooks/shopify"
SECRET = "test-client-secret"
KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


# --- fixtures ----------------------------------------------------------------------------------------


@pytest.fixture()
def fake() -> FakeShopify:
    return FakeShopify()


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def shop_settings(integration_settings):
    s = integration_settings
    s.shopify_client_id = "test-client-id"
    s.shopify_client_secret = SecretStr(SECRET)
    s.shopify_redirect_uri = "http://localhost:3000/integrations/shopify/callback"
    s.shopify_webhook_base_url = "https://api.example.com"
    s.secret_encryption_key = SecretStr(KEY)
    s.shopify_sync_min_interval_seconds = 0
    s.shopify_max_retries = 3
    return s


@pytest.fixture()
def runtime(shop_settings, fake, clock) -> ShopifyRuntime:
    return ShopifyRuntime(
        settings=shop_settings,
        secrets=SecretManager.from_settings(shop_settings),
        transport=fake.transport(),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        now=clock.now,
    )


@pytest.fixture()
def api(pg_client, runtime, pg_factory, redis_queue, two_tenants, fake):
    """The real app on Postgres + Redis with Shopify pointed at the fake."""
    from app.api.integrations.shopify import get_shopify_runtime

    app.dependency_overrides[get_shopify_runtime] = lambda: runtime
    app.dependency_overrides[get_session_factory] = lambda: pg_factory
    (ha, oa), (hb, ob) = two_tenants
    return type(
        "Api",
        (),
        {
            "c": pg_client,
            "a": ha,
            "b": hb,
            "org_a": uuid.UUID(oa["id"]),
            "org_b": uuid.UUID(ob["id"]),
            "sf": pg_factory,
            "rt": runtime,
            "q": redis_queue,
            "fake": fake,
        },
    )


def new_org(pg, slug) -> uuid.UUID:
    org = Organization(name=slug.title(), slug=slug)
    pg.add(org)
    pg.commit()
    return org.id


def product(number=1, **kw) -> mapper.ProductData:
    node = make_product(number, **kw)
    variants = node.pop("_variants")
    node["variants"] = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": variants}
    result = mapper.map_product(node)
    assert result is not None
    return result


def customer(number=1, **kw) -> mapper.CustomerData:
    result = mapper.map_customer(make_customer(number, **kw))
    assert result is not None
    return result


def order(number=1, **kw) -> mapper.OrderData:
    node = make_order(number, **kw)
    items = node.pop("_items")
    node["lineItems"] = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": items}
    result = mapper.map_order(node)
    assert result is not None
    return result


def count(pg, model, **where) -> int:
    pg.expire_all()
    stmt = select(func.count()).select_from(model)
    for key, value in where.items():
        stmt = stmt.where(getattr(model, key) == value)
    return int(pg.scalar(stmt))


def in_threads(n, target):
    """Run ``target(i)`` in ``n`` threads released together; re-raises the first failure."""
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []
    results: list = [None] * n

    def run(i):
        try:
            barrier.wait(timeout=10)
            results[i] = target(i)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    if errors:
        raise errors[0]
    return results


# --- tenant isolation is enforced by the database, not just by queries -----------------------------


class TwoOrgs:
    """Org A has a full product/customer/order; org B has its own product."""

    def __init__(self, pg):
        self.pg = pg
        self.a = new_org(pg, "alpha")
        self.b = new_org(pg, "beta")
        store.upsert_products(
            pg, self.a, [product(1, variants=[make_variant(11)])], currency="USD", now=NOW
        )
        store.upsert_products(
            pg, self.b, [product(2, variants=[make_variant(22)])], currency="USD", now=NOW
        )
        store.upsert_customers(pg, self.a, [customer(1)], now=NOW)
        store.upsert_orders(
            pg,
            self.a,
            [order(1, customer=1, items=[make_line_item(901, product=1, variant=11)])],
            now=NOW,
        )
        pg.commit()
        self.pa = pg.scalar(select(Product).where(Product.organization_id == self.a))
        self.pb = pg.scalar(select(Product).where(Product.organization_id == self.b))
        self.va = pg.scalar(select(ProductVariant).where(ProductVariant.organization_id == self.a))
        self.ca = pg.scalar(select(Customer).where(Customer.organization_id == self.a))
        self.oa = pg.scalar(select(Order).where(Order.organization_id == self.a))


@pytest.fixture()
def orgs(pg):
    return TwoOrgs(pg)


def rejected(pg, obj):
    pg.add(obj)
    with pytest.raises(IntegrityError):
        pg.commit()
    pg.rollback()


def test_a_variant_cannot_belong_to_another_organizations_product(pg, orgs):
    rejected(
        pg,
        ProductVariant(
            organization_id=orgs.b,
            product_id=orgs.pa.id,
            shopify_variant_id=555,
            title="x",
            synced_at=NOW,
        ),
    )


def test_an_order_cannot_point_at_another_organizations_customer(pg, orgs):
    rejected(
        pg,
        Order(
            organization_id=orgs.b,
            shopify_order_id=777,
            customer_id=orgs.ca.id,
            synced_at=NOW,
        ),
    )


def test_an_order_line_cannot_join_another_organizations_order_product_or_variant(pg, orgs):
    own = Order(organization_id=orgs.b, shopify_order_id=888, synced_at=NOW)
    pg.add(own)
    pg.commit()
    for number, foreign in enumerate(
        (
            {"order_id": orgs.oa.id},  # another org's order
            {"product_id": orgs.pa.id},  # another org's product
            {"variant_id": orgs.va.id},  # another org's variant
        )
    ):
        fields = {
            "organization_id": orgs.b,
            "order_id": own.id,
            "shopify_line_item_id": 4_000_000 + number,
            "title": "x",
            "quantity": 1,
        }
        rejected(pg, OrderItem(**{**fields, **foreign}))
    ok = OrderItem(
        organization_id=orgs.b, order_id=own.id, shopify_line_item_id=1, title="x", quantity=1
    )
    pg.add(ok)
    pg.commit()  # the same line is fine once it stays inside the tenant


def test_sync_progress_and_webhook_events_cannot_reference_another_organizations_connection(
    pg, orgs, runtime, fake, pg_factory
):
    connection = seed_connection(pg_factory, orgs.a, runtime, fake)
    rejected(
        pg,
        ShopifySyncState(
            organization_id=orgs.b, connection_id=connection, resource=SyncResource.PRODUCTS
        ),
    )
    rejected(
        pg,
        ShopifyWebhookEvent(
            shop_domain="x.myshopify.com",
            webhook_id="w",
            topic="products/update",
            organization_id=orgs.b,
            connection_id=connection,
            received_at=NOW,
        ),
    )


def test_the_same_shopify_id_is_allowed_in_two_organizations_but_never_twice_in_one(pg, orgs):
    store.upsert_products(pg, orgs.b, [product(1)], currency="USD", now=NOW)  # id 1 is org A's too
    pg.commit()
    assert count(pg, Product, shopify_product_id=1) == 2
    rejected(
        pg,
        Product(
            organization_id=orgs.a,
            shopify_product_id=1,
            title="duplicate",
            synced_at=NOW,
        ),
    )
    rejected(
        pg,
        Customer(organization_id=orgs.a, shopify_customer_id=1),
    )
    rejected(
        pg,
        Order(organization_id=orgs.a, shopify_order_id=1, synced_at=NOW),
    )


def test_one_organization_can_hold_only_one_connection_and_a_store_only_one_owner(
    pg, runtime, fake, pg_factory
):
    a, b = new_org(pg, "alpha"), new_org(pg, "beta")
    seed_connection(pg_factory, a, runtime, fake)
    with pytest.raises(IntegrityError):  # same org, second store
        seed_connection(pg_factory, a, runtime, fake, shop="second.myshopify.com")
    with pytest.raises(IntegrityError):  # same store, second organization
        seed_connection(pg_factory, b, runtime, fake)
    other = seed_connection(pg_factory, b, runtime, fake, shop="beta-store.myshopify.com")
    assert other is not None


def test_a_disconnected_store_can_be_claimed_by_another_organization(pg, runtime, fake, pg_factory):
    a, b = new_org(pg, "alpha"), new_org(pg, "beta")
    first = seed_connection(pg_factory, a, runtime, fake)
    with pg_factory() as s:
        s.get(ShopifyConnection, first).status = ShopifyConnectionStatus.DISCONNECTED
        s.commit()
    assert seed_connection(pg_factory, b, runtime, fake) is not None  # partial unique index


def test_deleting_an_organization_removes_all_of_its_shopify_data_and_nothing_else(
    pg, orgs, runtime, fake, pg_factory
):
    seed_connection(pg_factory, orgs.a, runtime, fake)
    pg.execute(text("DELETE FROM organizations WHERE id = :o"), {"o": orgs.a})
    pg.commit()
    for model in (Product, ProductVariant, Customer, Order, OrderItem, ShopifyConnection):
        assert count(pg, model, organization_id=orgs.a) == 0, model
    assert count(pg, Product, organization_id=orgs.b) == 1  # the other tenant is untouched
    assert count(pg, ProductVariant, organization_id=orgs.b) == 1


def test_the_read_service_never_returns_another_organizations_rows(pg, orgs):
    assert [p.shopify_product_id for p in queries.list_products(pg, orgs.a)] == [1]
    assert [p.shopify_product_id for p in queries.list_products(pg, orgs.b)] == [2]
    assert queries.get_product(pg, orgs.b, orgs.pa.id) is None
    assert queries.get_order(pg, orgs.b, orgs.oa.id) is None
    assert queries.get_customer(pg, orgs.b, orgs.ca.id) is None
    assert list(queries.list_variants(pg, orgs.b, orgs.pa.id)) == []
    assert list(queries.list_order_items(pg, orgs.b, orgs.oa.id)) == []
    assert list(queries.orders_for_customer(pg, orgs.b, orgs.ca.id)) == []
    assert queries.get_product(pg, orgs.a, orgs.pa.id) is not None  # and A still sees its own


# --- upserts on real Postgres -------------------------------------------------------------------------


def test_repeated_upserts_never_duplicate_and_children_stay_attached(pg):
    org = new_org(pg, "alpha")
    for _ in range(3):
        store.upsert_products(
            pg,
            org,
            [
                product(n, variants=[make_variant(n * 10 + 1), make_variant(n * 10 + 2)])
                for n in range(1, 8)
            ],
            currency="USD",
            now=NOW,
        )
        pg.commit()
    assert count(pg, Product) == 7 and count(pg, ProductVariant) == 14
    ids_before = set(pg.scalars(select(Product.id)))
    store.upsert_products(pg, org, [product(1)], currency="USD", now=NOW + timedelta(hours=1))
    pg.commit()
    assert set(pg.scalars(select(Product.id))) == ids_before  # a row keeps its primary key


def test_orders_upsert_keeps_line_items_unique_and_links_them_to_catalog_rows(pg):
    org = new_org(pg, "alpha")
    store.upsert_products(
        pg, org, [product(1, variants=[make_variant(11)])], currency="USD", now=NOW
    )
    store.upsert_customers(pg, org, [customer(3)], now=NOW)
    pg.commit()
    data = [
        order(
            n,
            customer=3,
            items=[
                make_line_item(n * 100 + 1, product=1, variant=11),
                make_line_item(n * 100 + 2, product=None, variant=None),
            ],
        )
        for n in range(1, 4)
    ]
    for _ in range(2):
        store.upsert_orders(pg, org, data, now=NOW)
        pg.commit()
    assert count(pg, Order) == 3 and count(pg, OrderItem) == 6
    linked = pg.scalar(
        select(func.count()).select_from(OrderItem).where(OrderItem.product_id.is_not(None))
    )
    assert linked == 3  # the catalog link resolved, the unknown line stayed unlinked
    assert (
        pg.scalar(select(func.count()).select_from(Order).where(Order.customer_id.is_not(None)))
        == 3
    )


def test_a_stale_update_never_overwrites_newer_data(pg):
    org = new_org(pg, "alpha")
    newer = product(1, title="Newer", updated="2026-10-21T10:00:00Z")
    older = product(1, title="Older", updated="2026-10-20T10:00:00Z")
    store.upsert_products(pg, org, [newer], currency="USD", now=NOW)
    pg.commit()
    result = store.upsert_products(pg, org, [older], currency="USD", now=NOW)
    pg.commit()
    assert (result.written, result.stale) == (0, 1)
    assert pg.scalar(select(Product.title)) == "Newer"


def test_concurrent_upserts_of_the_same_objects_never_duplicate_or_fail(pg_factory, pg):
    org = new_org(pg, "alpha")
    data = [product(n, variants=[make_variant(n * 10 + 1)]) for n in range(1, 21)]

    def writer(_i):
        with pg_factory() as s:
            store.upsert_products(s, org, data, currency="USD", now=NOW)
            s.commit()

    in_threads(6, writer)
    assert count(pg, Product) == 20 and count(pg, ProductVariant) == 20


def test_reconciliation_soft_deletes_only_what_the_run_did_not_see(pg):
    org, other = new_org(pg, "alpha"), new_org(pg, "beta")
    store.upsert_products(pg, org, [product(1), product(2)], currency="USD", now=NOW)
    store.upsert_products(pg, other, [product(1)], currency="USD", now=NOW)
    pg.commit()
    store.upsert_products(
        pg, org, [product(1)], currency="USD", now=NOW + timedelta(hours=2)
    )  # product 2 was not seen in this run
    pg.commit()
    removed = store.soft_delete_unseen(
        pg, org, products=True, customers=True, since=NOW + timedelta(hours=1)
    )
    pg.commit()
    assert removed["products"] == 1
    pg.expire_all()
    assert {
        p.shopify_product_id: p.deleted_at is None
        for p in pg.scalars(select(Product).where(Product.organization_id == org))
    } == {1: True, 2: False}
    assert (
        pg.scalar(select(Product.deleted_at).where(Product.organization_id == other)) is None
    )  # another tenant's rows are never reconciled


def test_purge_removes_everything_for_one_organization_in_batches(pg):
    a, b = new_org(pg, "alpha"), new_org(pg, "beta")
    for org in (a, b):
        store.upsert_products(
            pg,
            org,
            [product(n, variants=[make_variant(n * 10)]) for n in range(1, 8)],
            currency="USD",
            now=NOW,
        )
        store.upsert_customers(pg, org, [customer(n) for n in range(1, 6)], now=NOW)
        store.upsert_orders(
            pg,
            org,
            [
                order(n, customer=n, items=[make_line_item(n * 100, product=n, variant=None)])
                for n in range(1, 6)
            ],
            now=NOW,
        )
        pg.commit()
    removed = store.purge_shop_data(pg, a, batch=3)
    assert removed == {
        "order_items": 5,
        "orders": 5,
        "customers": 5,
        "product_variants": 7,
        "products": 7,
    }
    for model in (Product, ProductVariant, Customer, Order, OrderItem):
        assert count(pg, model, organization_id=a) == 0
    assert store.commerce_counts(pg, b).products == 7 and store.commerce_counts(pg, b).orders == 5


# --- webhook idempotency through the real unique constraint ---------------------------------------------


def signed(topic, payload, webhook_id, shop="test-shop.myshopify.com"):
    body = json.dumps(payload).encode()
    headers = {
        "x-shopify-topic": topic,
        "x-shopify-shop-domain": shop,
        "x-shopify-webhook-id": webhook_id,
        "x-shopify-hmac-sha256": sign_webhook_body(body, SECRET),
    }
    return body, headers


def test_the_database_itself_refuses_a_second_event_with_the_same_delivery_id(pg):
    def event(**kw):
        return ShopifyWebhookEvent(
            shop_domain="test-shop.myshopify.com",
            webhook_id="wh-1",
            topic="products/update",
            received_at=NOW,
            **kw,
        )

    pg.add(event())
    pg.commit()
    rejected(pg, event())
    other_shop = ShopifyWebhookEvent(
        shop_domain="other.myshopify.com",
        webhook_id="wh-1",
        topic="products/update",
        received_at=NOW,
    )
    pg.add(other_shop)
    pg.commit()  # the same id from a different shop is a different delivery


def test_simultaneous_deliveries_of_one_webhook_produce_one_event_and_one_job(
    pg, pg_factory, runtime, fake, shop_settings, redis_queue
):
    org = new_org(pg, "alpha")
    seed_connection(pg_factory, org, runtime, fake)
    body, headers = signed("products/update", {"id": 5}, "wh-race")

    def deliver(_i):
        with pg_factory() as s:
            return webhooks.receive_webhook(
                s, body=body, headers=headers, settings=shop_settings, queue=redis_queue
            ).status

    outcomes = in_threads(8, deliver)
    assert sorted(outcomes) == ["accepted"] + ["duplicate"] * 7
    assert count(pg, ShopifyWebhookEvent) == 1
    assert redis_queue.length() == 1  # processed once, however many times Shopify sent it


class _BlindToDuplicates:
    """A session that misses the 'already seen?' lookup, as a delivery racing in between would."""

    def __init__(self, session):
        self._session = session

    def scalar(self, stmt, *a, **kw):
        if "shopify_webhook_events" in str(stmt):
            return None
        return self._session.scalar(stmt, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._session, name)


def test_a_race_lost_at_the_unique_constraint_is_reported_as_a_duplicate_not_an_error(
    pg, pg_factory, runtime, fake, shop_settings, redis_queue
):
    org = new_org(pg, "alpha")
    seed_connection(pg_factory, org, runtime, fake)
    body, headers = signed("products/update", {"id": 5}, "wh-lost")
    with pg_factory() as s:
        first = webhooks.receive_webhook(
            s, body=body, headers=headers, settings=shop_settings, queue=redis_queue
        )
    assert first.status == "accepted"
    with pg_factory() as s:
        second = webhooks.receive_webhook(
            _BlindToDuplicates(s),
            body=body,
            headers=headers,
            settings=shop_settings,
            queue=redis_queue,
        )
    assert second.status == "duplicate"
    assert count(pg, ShopifyWebhookEvent) == 1 and redis_queue.length() == 1


def test_a_webhook_event_is_processed_exactly_once_even_if_two_workers_get_it(
    pg, pg_factory, runtime, fake, shop_settings, redis_queue
):
    org = new_org(pg, "alpha")
    seed_connection(pg_factory, org, runtime, fake)
    fake.products = [make_product(5)]
    body, headers = signed("products/update", {"id": 5}, "wh-once")
    with pg_factory() as s:
        webhooks.receive_webhook(
            s, body=body, headers=headers, settings=shop_settings, queue=redis_queue
        )
    event_id = pg.scalar(select(ShopifyWebhookEvent.id))

    results = in_threads(4, lambda _i: webhooks.process_event(runtime, pg_factory, event_id))
    assert sorted(results).count("processed") == 1  # the claim is an atomic UPDATE
    assert count(pg, Product) == 1
    assert pg.scalar(select(ShopifyWebhookEvent.status)) == WebhookEventStatus.PROCESSED
    assert fake.operations() == ["Product"]  # one fetch, by the one worker that claimed the event


# --- the sync state machine --------------------------------------------------------------------------


def fill_store(fake, products=12, customers=7, orders=9):
    fake.products = [
        make_product(n, variants=[make_variant(n * 10 + 1), make_variant(n * 10 + 2)])
        for n in range(1, products + 1)
    ]
    fake.customers = [make_customer(n) for n in range(1, customers + 1)]
    fake.orders = [
        make_order(
            n,
            customer=(n % customers) + 1,
            items=[make_line_item(n * 100 + 1, product=(n % products) + 1, variant=None)],
        )
        for n in range(1, orders + 1)
    ]


def begin(pg_factory, cid, runtime):
    with pg_factory() as db:
        sync.prepare_sync(db, db.get(ShopifyConnection, cid), runtime.now())
        db.commit()


def drain(runtime, pg_factory, cid, queue, rounds=30):
    outcome = ""
    for _ in range(rounds):
        outcome = sync.run_sync_job(runtime, pg_factory, cid, queue=queue)
        if outcome != "continued":
            return outcome
    raise AssertionError("sync did not finish")


def test_a_full_sync_imports_everything_and_leaves_every_stage_completed(
    pg, pg_factory, runtime, fake, redis_queue, shop_settings, clock
):
    shop_settings.shopify_sync_slice_seconds = 15
    org = new_org(pg, "alpha")
    cid = seed_connection(pg_factory, org, runtime, fake)
    fill_store(fake, products=70)  # four pages of products

    def slow(request):
        clock.advance(10)  # every request takes ten "seconds", so a slice holds one page
        return fake.handler(request)

    runtime.transport = httpx.MockTransport(slow)
    begin(pg_factory, cid, runtime)

    resumed = 0
    outcome = ""
    for _ in range(60):
        outcome = sync.run_sync_job(runtime, pg_factory, cid, queue=redis_queue)
        if outcome != "continued":
            break
        job = redis_queue.dequeue(timeout=1)
        assert isinstance(job, ShopifySyncJob) and job.connection_id == cid
        resumed += 1
    assert outcome == "completed" and resumed >= 2  # picked up again from a saved cursor

    counts = store.commerce_counts(pg, org)
    assert (counts.products, counts.variants, counts.customers, counts.orders) == (70, 140, 7, 9)
    pg.expire_all()
    states = {s.resource: s for s in pg.scalars(select(ShopifySyncState))}
    assert {r: s.status for r, s in states.items()} == {
        SyncResource.PRODUCTS: SyncStatus.COMPLETED,
        SyncResource.CUSTOMERS: SyncStatus.COMPLETED,
        SyncResource.ORDERS: SyncStatus.COMPLETED,
    }
    assert states[SyncResource.PRODUCTS].items_synced == 70
    connection = pg.get(ShopifyConnection, cid)
    assert connection.status == ShopifyConnectionStatus.CONNECTED and connection.last_sync_at


def test_running_the_sync_again_changes_nothing(pg, pg_factory, runtime, fake, redis_queue):
    org = new_org(pg, "alpha")
    cid = seed_connection(pg_factory, org, runtime, fake)
    fill_store(fake)
    for _ in range(2):
        begin(pg_factory, cid, runtime)
        assert drain(runtime, pg_factory, cid, redis_queue) == "completed"
    counts = store.commerce_counts(pg, org)
    assert (counts.products, counts.variants, counts.customers, counts.orders) == (12, 24, 7, 9)
    assert count(pg, OrderItem) == 9
    assert redis_queue.length() == 0


def test_two_workers_given_the_same_sync_do_not_import_twice(
    pg, pg_factory, runtime, fake, redis_queue
):
    org = new_org(pg, "alpha")
    cid = seed_connection(pg_factory, org, runtime, fake)
    fill_store(fake)
    begin(pg_factory, cid, runtime)

    outcomes = in_threads(
        3, lambda _i: sync.run_sync_job(runtime, pg_factory, cid, queue=redis_queue)
    )
    assert "completed" in outcomes
    counts = store.commerce_counts(pg, org)
    assert (counts.products, counts.customers, counts.orders) == (12, 7, 9)
    pg.expire_all()
    assert all(s.status == SyncStatus.COMPLETED for s in pg.scalars(select(ShopifySyncState)))
    assert (
        pg.scalar(
            select(ShopifySyncState.items_synced).where(
                ShopifySyncState.resource == SyncResource.PRODUCTS
            )
        )
        == 12
    )  # not 24: one worker did each stage


def test_a_crashed_worker_leaves_a_stage_that_maintenance_recovers(
    pg, pg_factory, runtime, fake, redis_queue
):
    org = new_org(pg, "alpha")
    cid = seed_connection(pg_factory, org, runtime, fake)
    fill_store(fake)
    begin(pg_factory, cid, runtime)
    with pg_factory() as s:  # the worker claimed PRODUCTS and then died without a heartbeat
        s.execute(
            update(ShopifySyncState)
            .where(ShopifySyncState.resource == SyncResource.PRODUCTS)
            .values(
                status=SyncStatus.RUNNING,
                heartbeat_at=datetime.now(UTC) - timedelta(hours=1),
                attempts=1,
            )
        )
        s.commit()
    assert sync.reap_stale_sync_states(pg_factory, redis_queue, 60) >= 1
    assert isinstance(redis_queue.dequeue(timeout=1), ShopifySyncJob)  # work was re-queued
    assert drain(runtime, pg_factory, cid, redis_queue) == "completed"
    assert store.commerce_counts(pg, org).products == 12


def test_a_missing_scope_skips_the_stage_instead_of_failing_the_sync(
    pg, pg_factory, runtime, fake, redis_queue
):
    org = new_org(pg, "alpha")
    cid = seed_connection(pg_factory, org, runtime, fake, scopes="read_products")
    fill_store(fake)
    begin(pg_factory, cid, runtime)
    assert drain(runtime, pg_factory, cid, redis_queue) == "completed"
    pg.expire_all()
    status = {s.resource: s.status for s in pg.scalars(select(ShopifySyncState))}
    assert status[SyncResource.PRODUCTS] == SyncStatus.COMPLETED
    assert status[SyncResource.CUSTOMERS] == SyncStatus.SKIPPED
    assert status[SyncResource.ORDERS] == SyncStatus.SKIPPED
    assert store.commerce_counts(pg, org).customers == 0


# --- token refresh: one rotation, however many workers need it --------------------------------------------


def test_workers_racing_to_refresh_an_expired_token_rotate_it_exactly_once(
    pg, pg_factory, runtime, fake, clock
):
    org = new_org(pg, "alpha")
    cid = seed_connection(pg_factory, org, runtime, fake)
    clock.advance(3550)  # inside the safety margin: every worker sees an expired token

    tokens = in_threads(5, lambda _i: runtime.tokens_for(pg_factory, cid).access_token())
    assert len(set(tokens)) == 1  # everyone ends up with the same, current token
    assert (
        len(fake.token_requests) == 1
    )  # the refresh token rotates, so a 2nd refresh would break it
    assert tokens[0] in fake.valid_access


def test_workers_that_all_got_a_401_with_the_same_token_rotate_it_once(
    pg, pg_factory, runtime, fake
):
    org = new_org(pg, "alpha")
    cid = seed_connection(pg_factory, org, runtime, fake)
    sources = [runtime.tokens_for(pg_factory, cid) for _ in range(4)]
    stale = {s.access_token() for s in sources}
    assert len(stale) == 1
    fresh = in_threads(4, lambda i: sources[i].force_refresh())
    assert len(set(fresh)) == 1 and fresh[0] not in stale
    assert len(fake.token_requests) == 1


# --- the API on Postgres and Redis ---------------------------------------------------------------------------


def connect(api, headers, org_id):
    started = api.c.post(BASE + "/connect", json={"shop": api.fake.shop}, headers=headers)
    assert started.status_code == 200, started.text
    query = callback_query(api.fake, state_from(started.json()["authorization_url"]))
    done = api.c.get(f"{BASE}/callback?{urlencode(query)}", headers=headers)
    assert done.status_code == 200, done.text
    return done.json()


def test_connect_sync_and_webhook_flow_on_postgres_and_redis(api, pg, shop_settings):
    fill_store(api.fake)
    done = connect(api, api.a, api.org_a)
    assert done["shop_domain"] == api.fake.shop

    # the connection is stored encrypted and tenant-bound; the token is nowhere in the response
    row = pg.scalar(select(ShopifyConnection).where(ShopifyConnection.organization_id == api.org_a))
    assert (
        row.access_token_encrypted.startswith("bf1:") and "shpat_" not in row.access_token_encrypted
    )

    queued = api.q.dequeue(timeout=2)
    assert isinstance(queued, ShopifySyncJob) and queued.connection_id == row.id
    assert sync.run_sync_job(api.rt, api.sf, row.id, queue=api.q) == "completed"

    status = api.c.get(BASE, headers=api.a).json()
    assert status["connection"]["status"] == "CONNECTED"
    assert status["counts"] == {"products": 12, "variants": 24, "customers": 7, "orders": 9}
    assert [s["status"] for s in status["sync"]] == ["COMPLETED"] * 3
    assert "shpat_" not in json.dumps(status) and "bf1:" not in json.dumps(status)

    # a signed webhook is accepted at once, queued in Redis, and applied by the worker
    api.fake.products[0]["title"] = "Renamed by merchant"
    api.fake.products[0]["updatedAt"] = "2030-01-01T00:00:00Z"
    body, headers = signed("products/update", {"id": 1}, "wh-api-1", shop=api.fake.shop)
    first = api.c.post(HOOK, content=body, headers=headers)
    again = api.c.post(HOOK, content=body, headers=headers)
    assert (first.json(), again.json()) == ({"status": "accepted"}, {"status": "duplicate"})
    job = api.q.dequeue(timeout=2)
    assert isinstance(job, ShopifyWebhookJob) and api.q.length() == 0
    assert webhooks.process_event(api.rt, api.sf, job.event_id) == "processed"
    pg.expire_all()
    assert (
        pg.scalar(
            select(Product.title).where(
                Product.organization_id == api.org_a, Product.shopify_product_id == 1
            )
        )
        == "Renamed by merchant"
    )

    # a forged one is refused and leaves no trace
    forged = dict(headers, **{"x-shopify-hmac-sha256": sign_webhook_body(body, "wrong")})
    assert api.c.post(HOOK, content=body, headers=forged).status_code == 401
    assert count(pg, ShopifyWebhookEvent) == 1


def test_a_second_tenant_sees_and_touches_nothing_of_the_first(api, pg):
    fill_store(api.fake)
    connect(api, api.a, api.org_a)
    cid = pg.scalar(
        select(ShopifyConnection.id).where(ShopifyConnection.organization_id == api.org_a)
    )
    assert api.q.dequeue(timeout=2) is not None
    sync.run_sync_job(api.rt, api.sf, cid, queue=api.q)

    seen_by_b = api.c.get(BASE, headers=api.b).json()
    assert seen_by_b["connection"] is None
    assert seen_by_b["counts"] == {"products": 0, "variants": 0, "customers": 0, "orders": 0}
    assert api.c.post(BASE + "/sync", headers=api.b).status_code == 404
    assert api.c.post(BASE + "/disconnect", headers=api.b).status_code == 404
    # B cannot name A's organization to reach A's connection
    stolen = api.c.get(BASE, headers={**api.b, "X-Organization-ID": str(api.org_a)})
    assert stolen.status_code in (403, 404)
    assert api.c.get(BASE, headers=api.a).json()["counts"]["products"] == 12  # A is unaffected


def test_disconnect_purges_the_data_and_releases_the_store_on_postgres(api, pg):
    fill_store(api.fake)
    connect(api, api.a, api.org_a)
    cid = pg.scalar(
        select(ShopifyConnection.id).where(ShopifyConnection.organization_id == api.org_a)
    )
    api.q.dequeue(timeout=2)
    sync.run_sync_job(api.rt, api.sf, cid, queue=api.q)

    r = api.c.post(BASE + "/disconnect", headers=api.a)
    assert r.status_code == 200 and r.json()["connection"]["status"] == "DISCONNECTED"
    pg.expire_all()
    row = pg.get(ShopifyConnection, cid)
    assert row.access_token_encrypted is None and row.refresh_token_encrypted is None
    assert count(pg, Product) == 0 and count(pg, Order) == 0 and count(pg, ShopifySyncState) == 0

    # the same store can now be connected by the other organization
    api.fake.valid_codes.add("good-code")
    done = connect(api, api.b, api.org_b)
    assert done["shop_domain"] == api.fake.shop


def test_only_the_first_of_two_racing_sync_requests_is_accepted(api, pg):
    connect(api, api.a, api.org_a)
    api.q.dequeue(timeout=2)
    cid = pg.scalar(
        select(ShopifyConnection.id).where(ShopifyConnection.organization_id == api.org_a)
    )
    sync.run_sync_job(api.rt, api.sf, cid, queue=api.q)

    statuses = in_threads(5, lambda _i: api.c.post(BASE + "/sync", headers=api.a).status_code)
    assert sorted(statuses) == [202, 409, 409, 409, 409]
    assert api.q.length() == 1


# --- the job queue on real Redis -----------------------------------------------------------------------------


def test_webhooks_are_served_before_crawls_and_crawls_before_bulk_syncs(redis_queue):
    cid, sid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    redis_queue.enqueue(ShopifySyncJob(cid))
    redis_queue.enqueue(CrawlJob(sid))
    redis_queue.enqueue(ShopifyWebhookJob(eid))
    redis_queue.enqueue(ShopifyWebhookJob(eid))
    order_out = [type(redis_queue.dequeue(timeout=1)).__name__ for _ in range(4)]
    assert order_out == ["ShopifyWebhookJob", "ShopifyWebhookJob", "CrawlJob", "ShopifySyncJob"]
    assert redis_queue.dequeue(timeout=1) is None


def test_jobs_round_trip_through_redis_with_their_ids(redis_queue):
    cid, eid = uuid.uuid4(), uuid.uuid4()
    redis_queue.enqueue(ShopifySyncJob(cid))
    redis_queue.enqueue(ShopifyWebhookJob(eid))
    first, second = redis_queue.dequeue(timeout=1), redis_queue.dequeue(timeout=1)
    assert first == ShopifyWebhookJob(eid) and second == ShopifySyncJob(cid)


def test_a_malformed_job_payload_is_discarded_and_does_not_block_the_queue(redis_queue):
    redis_queue._client.rpush(redis_queue.keys()[1], "not json")
    redis_queue._client.rpush(redis_queue.keys()[1], json.dumps({"type": "something_else"}))
    redis_queue.enqueue(CrawlJob(uuid.uuid4()))
    assert redis_queue.dequeue(timeout=1) is None  # malformed
    assert redis_queue.dequeue(timeout=1) is None  # unknown type
    assert isinstance(redis_queue.dequeue(timeout=1), CrawlJob)
    assert parse_job("{}") is None and parse_job("[") is None


def test_the_test_redis_is_the_isolated_database_not_the_development_one():
    assert TEST_REDIS_URL.rstrip("/").endswith("/15")
    assert isinstance(RedisJobQueue, type)


def test_two_simultaneous_callbacks_with_the_same_state_connect_once(
    api, pg, shop_settings, monkeypatch
):
    """The state is consumed by one atomic UPDATE, so of N racing completions exactly one proceeds
    to the token exchange; without it, each would exchange the code and store a connection.

    Threads alone rarely overlap here (the first finishes before the others start), so the race is
    forced: no thread may try to consume the state until every thread has read it as unused."""
    from app.services.integrations.shopify import oauth
    from app.services.integrations.shopify.errors import OAuthError
    from app.services.integrations.shopify.validation import parse_query_string

    everyone_has_read_the_state = threading.Barrier(5, timeout=20)
    arrived: set[int] = set()
    real_aware = oauth._aware

    def aware_after_everyone_has_read(value):
        if threading.get_ident() not in arrived:  # the first call follows the state SELECT
            arrived.add(threading.get_ident())
            everyone_has_read_the_state.wait()
        return real_aware(value)

    monkeypatch.setattr(oauth, "_aware", aware_after_everyone_has_read)

    me = api.c.get("/api/auth/me", headers=api.a).json()
    started = api.c.post(BASE + "/connect", json={"shop": api.fake.shop}, headers=api.a)
    query = callback_query(api.fake, state_from(started.json()["authorization_url"]))
    query = parse_query_string(urlencode(query))

    def complete(_i):
        with api.sf() as s:
            try:
                oauth.complete_authorization(
                    s,
                    organization_id=api.org_a,
                    user_id=uuid.UUID(me["id"]),
                    query=query,
                    settings=shop_settings,
                    secrets_manager=api.rt.secrets,
                    client_for=api.rt.client_for,
                    now=datetime.now(UTC),
                )
                return "connected"
            except OAuthError as exc:
                return exc.code

    outcomes = in_threads(5, complete)
    assert sorted(outcomes) == ["connected"] + ["invalid_state"] * 4, outcomes
    assert len(api.fake.token_requests) == 1  # the code was exchanged once, not five times
    assert count(pg, ShopifyConnection) == 1


# --- disconnecting while a page is being written -----------------------------------------------------------


def test_a_disconnect_during_a_page_write_waits_for_it_and_then_leaves_nothing_behind(
    pg, pg_factory, runtime, fake, redis_queue, monkeypatch
):
    """The page is written inside a transaction that has the stage row locked. A disconnect must
    wait for that page and then purge it; it must not slip in between the worker's last check and
    its commit, which would leave a disconnected shop's customers in the database."""
    from app.services.integrations.shopify import lifecycle

    org = new_org(pg, "alpha")
    cid = seed_connection(pg_factory, org, runtime, fake)
    fill_store(fake, products=3, customers=0, orders=0)
    begin(pg_factory, cid, runtime)

    in_write, proceed = threading.Event(), threading.Event()
    original = store.upsert_products

    def slow_write(db, org_id, items, **kwargs):
        result = original(db, org_id, items, **kwargs)  # the page is staged in the open transaction
        in_write.set()
        assert proceed.wait(30)
        return result

    monkeypatch.setattr(store, "upsert_products", slow_write)
    errors: list[BaseException] = []

    def guarded(fn):
        def run():
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - surfaced by the main thread below
                errors.append(exc)

        return threading.Thread(target=run, daemon=True)

    worker = guarded(lambda: sync.run_sync_job(runtime, pg_factory, cid, queue=redis_queue))
    worker.start()
    assert in_write.wait(30), "the worker never reached its page write"

    disconnect = guarded(
        lambda: lifecycle.disconnect_store(runtime, pg_factory, cid, remove_webhooks=False)
    )
    disconnect.start()
    disconnect.join(2.0)
    assert disconnect.is_alive(), "the disconnect did not wait for the page being written"

    proceed.set()
    worker.join(60)
    disconnect.join(60)
    assert not worker.is_alive() and not disconnect.is_alive() and errors == []

    pg.expire_all()
    assert count(pg, Product) == 0 and count(pg, ProductVariant) == 0
    assert count(pg, ShopifySyncState) == 0
    connection = pg.get(ShopifyConnection, cid)
    assert connection.status == ShopifyConnectionStatus.DISCONNECTED
    assert connection.access_token_encrypted is None and connection.refresh_token_encrypted is None


# --- a stuck token-refresh lock ---------------------------------------------------------------------------


def test_a_refresh_that_cannot_get_the_connection_lock_gives_up_as_a_transient_failure(
    pg, pg_factory, runtime, fake
):
    """A worker holding the connection row (a long transaction, a hung peer) must not make every other
    worker wait for ever: the server's lock timeout ends the wait, and it is reported as a transient
    'unavailable' that the sync's normal retry handling understands, not as a database crash."""
    from sqlalchemy.orm import sessionmaker

    from app.db.database import build_engine
    from app.services.integrations.shopify.errors import ShopifyUnavailable

    org = new_org(pg, "alpha")
    cid = seed_connection(pg_factory, org, runtime, fake)
    impatient = build_engine(
        runtime.settings.model_copy(
            update={"database_url": TEST_DATABASE_URL, "db_lock_timeout_seconds": 1}
        )
    )
    try:
        with pg_factory() as holder:
            holder.execute(
                select(ShopifyConnection).where(ShopifyConnection.id == cid).with_for_update()
            ).all()  # this transaction now owns the row and does not finish
            tokens = runtime.tokens_for(sessionmaker(bind=impatient), cid)
            started = time.monotonic()
            with pytest.raises(ShopifyUnavailable) as exc:
                tokens.refresh(force=True)
            assert time.monotonic() - started < 10
            assert exc.value.code == "unavailable"
            holder.rollback()
        assert fake.token_requests == []  # no token exchange was attempted while blocked
        # and once the lock is free the same call works
        runtime.tokens_for(pg_factory, cid).refresh(force=True)
        assert len(fake.token_requests) == 1
    finally:
        impatient.dispose()
