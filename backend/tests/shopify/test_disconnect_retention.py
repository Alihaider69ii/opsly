"""Disconnecting a store removes what was imported from it, for good.

The decision (kept from Phase 4): Disconnect PURGES imported Shopify commerce data. Everything is
re-importable from Shopify, and keeping a departed merchant's customers around is a liability. What
stays is audit metadata only: the connection row (no tokens) and the webhook ledger (identifiers).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Customer,
    Order,
    OrderItem,
    Product,
    ProductVariant,
    ShopifyConnection,
    ShopifyConnectionStatus,
    ShopifySyncState,
    ShopifyWebhookEvent,
    WebhookRegistration,
)
from app.services.integrations.shopify import lifecycle, mapper, store, webhooks
from tests.shopify.conftest import connect_through_api, seed_connection
from tests.shopify.fake_shopify import (
    make_customer,
    make_line_item,
    make_order,
    make_product,
    make_variant,
)
from tests.shopify.test_sync import full_sync
from tests.shopify.test_webhooks import deliver

BASE = "/api/integrations/shopify"
PII = {
    "email": "ingrid.stormvale@retention-test.example",
    "phone": "+4915112345678",
    "first": "Ingrid",
    "last": "Stormvale",
    "webhook_email": "late.arrival@retention-test.example",
}
COMMERCE = (Product, ProductVariant, Customer, Order, OrderItem)


def fill(fake, customers=4):
    fake.products = [make_product(n, variants=[make_variant(n * 10 + 1)]) for n in range(1, 4)]
    fake.customers = [
        make_customer(
            n,
            email=PII["email"] if n == 1 else None,
            phone=PII["phone"] if n == 1 else None,
            first=PII["first"] if n == 1 else "Other",
            last=PII["last"] if n == 1 else "Person",
        )
        for n in range(1, customers + 1)
    ]
    fake.orders = [
        make_order(n, customer=1, items=[make_line_item(n * 10, product=1, variant=11)])
        for n in range(1, 4)
    ]


def counts(db, org=None) -> dict[str, int]:
    out = {}
    for model in COMMERCE:
        stmt = select(func.count()).select_from(model)
        if org is not None:
            stmt = stmt.where(model.organization_id == org)
        out[model.__tablename__] = int(db.scalar(stmt))
    return out


def database_text(db) -> str:
    """Every row of every table, as text: the strictest possible 'is it anywhere' check."""
    db.expire_all()
    parts = []
    for table in Base.metadata.sorted_tables:
        for row in db.execute(select(table)).all():
            parts.append(repr(tuple(row)))
    return "\n".join(parts)


@pytest.fixture()
def world(shopify_client, tenants, fake, runtime, session_factory, queue, db):
    a, b = tenants
    fill(fake)
    return type(
        "World",
        (),
        {
            "c": shopify_client,
            "a": a,
            "b": b,
            "org_a": uuid.UUID(a.org["id"]),
            "org_b": uuid.UUID(b.org["id"]),
            "fake": fake,
            "rt": runtime,
            "sf": session_factory,
            "q": queue,
            "db": db,
        },
    )


def connect_and_import(world, tenant="a"):
    headers = getattr(world, tenant).headers
    world.fake.valid_codes.add("good-code")  # Shopify issues a fresh single-use code per approval
    response = connect_through_api(world.c, headers, world.fake)
    assert response.status_code == 200, response.text
    with world.sf() as s:
        org = world.org_a if tenant == "a" else world.org_b
        cid = s.scalar(select(ShopifyConnection.id).where(ShopifyConnection.organization_id == org))
    assert full_sync(world.rt, world.sf, cid, world.q) == "completed"
    return cid


# --- what is left after a disconnect ---------------------------------------------------------------------------


def test_after_disconnect_no_imported_row_and_no_customer_detail_exists_anywhere_in_the_database(
    world,
):
    connect_and_import(world)
    before = database_text(world.db)
    for value in (PII["email"], PII["phone"], PII["first"], PII["last"]):
        assert value in before, f"the import should have stored {value!r}"

    response = world.c.post(f"{BASE}/disconnect", headers=world.a.headers)
    assert response.status_code == 200

    assert counts(world.db) == dict.fromkeys(counts(world.db), 0)
    after = database_text(world.db)
    for value in (PII["email"], PII["phone"], PII["first"], PII["last"]):
        assert value not in after, f"{value!r} survived the disconnect"


def test_only_audit_metadata_remains_and_it_holds_no_credentials(world):
    cid = connect_and_import(world)
    world.c.post(f"{BASE}/disconnect", headers=world.a.headers)
    world.db.expire_all()
    row = world.db.get(ShopifyConnection, cid)
    assert row.status == ShopifyConnectionStatus.DISCONNECTED
    assert row.access_token_encrypted is None and row.refresh_token_encrypted is None
    assert row.access_token_expires_at is None and row.refresh_token_expires_at is None
    assert row.webhook_status == WebhookRegistration.NOT_CONFIGURED
    assert row.error_code is None
    assert world.db.scalar(select(func.count()).select_from(ShopifySyncState)) == 0
    # the table that records what was received keeps identifiers only, so it can stay for audit
    assert {c.name for c in ShopifyWebhookEvent.__table__.columns}.isdisjoint(
        {"payload", "body", "data", "raw", "email", "phone", "name"}
    )


def test_another_organizations_imported_data_is_untouched(world):
    connect_and_import(world, "a")
    world.fake.shop = "second-shop.myshopify.com"
    seed_connection(world.sf, world.org_b, world.rt, world.fake, shop=world.fake.shop)
    with world.sf() as s:
        store.upsert_customers(
            s,
            world.org_b,
            [mapper.map_customer(make_customer(1))],
            now=datetime.now(UTC),
        )
        s.commit()
    b_before = counts(world.db, world.org_b)
    world.fake.shop = "test-shop.myshopify.com"
    world.c.post(f"{BASE}/disconnect", headers=world.a.headers)
    assert counts(world.db, world.org_a) == dict.fromkeys(COMMERCE_NAMES, 0)
    assert counts(world.db, world.org_b) == b_before and b_before["customers"] == 1


COMMERCE_NAMES = tuple(m.__tablename__ for m in COMMERCE)


# --- webhooks after the purge ---------------------------------------------------------------------------------


def test_a_webhook_for_a_disconnected_shop_is_acknowledged_but_stores_and_queues_nothing(world):
    connect_and_import(world)
    world.c.post(f"{BASE}/disconnect", headers=world.a.headers)
    world.q.jobs.clear()
    events_before = world.db.scalar(select(func.count()).select_from(ShopifyWebhookEvent))

    for topic in ("customers/update", "customers/create", "orders/create", "products/update"):
        r = deliver(
            world.c,
            topic,
            {"id": 1, "email": PII["webhook_email"], "first_name": PII["first"]},
            webhook_id=f"late-{topic}",
        )
        assert r.status_code == 200 and r.json() == {"status": "ignored"}, topic

    assert list(world.q.jobs) == []
    world.db.expire_all()
    assert world.db.scalar(select(func.count()).select_from(ShopifyWebhookEvent)) == events_before
    text = database_text(world.db)
    assert PII["webhook_email"] not in text and counts(world.db) == dict.fromkeys(COMMERCE_NAMES, 0)


# --- coming back ----------------------------------------------------------------------------------------------------


def test_reconnecting_imports_everything_again_without_duplicates_or_leftovers(world):
    connect_and_import(world)
    first = counts(world.db)
    assert first["customers"] == 4 and first["orders"] == 3

    world.c.post(f"{BASE}/disconnect", headers=world.a.headers)
    assert counts(world.db) == dict.fromkeys(COMMERCE_NAMES, 0)

    connect_and_import(world)
    assert counts(world.db) == first
    world.db.expire_all()
    ids = list(world.db.scalars(select(Customer.shopify_customer_id)))
    assert len(ids) == len(set(ids))
    assert world.db.scalar(select(func.count()).select_from(ShopifyConnection)) == 1  # row reused


# --- the safety net ------------------------------------------------------------------------------------------------


def disconnect_without_purge(world, cid):
    lifecycle.disconnect_store(world.rt, world.sf, cid, remove_webhooks=False, purge=False)


def age(world, cid, days):
    with world.sf() as s:
        s.execute(
            update(ShopifyConnection)
            .where(ShopifyConnection.id == cid)
            .values(updated_at=datetime.now(UTC) - timedelta(days=days))
        )
        s.commit()


def test_the_safety_net_finishes_an_interrupted_purge_however_long_ago_it_was(world):
    cid = connect_and_import(world)
    assert counts(world.db)["customers"] == 4
    disconnect_without_purge(world, cid)  # the process died before the purge ran
    age(world, cid, 400)

    assert lifecycle.finish_pending_purges(world.sf) == 1
    assert counts(world.db) == dict.fromkeys(COMMERCE_NAMES, 0)
    assert lifecycle.finish_pending_purges(world.sf) == 0  # nothing left to do


def test_the_safety_net_finishes_a_purge_that_stopped_halfway(world):
    cid = connect_and_import(world)
    disconnect_without_purge(world, cid)
    with world.sf() as s:  # children were removed, parents were not
        s.query(OrderItem).delete()
        s.query(Order).delete()
        s.commit()
    assert counts(world.db)["customers"] == 4
    assert lifecycle.finish_pending_purges(world.sf) == 1
    assert counts(world.db) == dict.fromkeys(COMMERCE_NAMES, 0)


def test_the_safety_net_removes_rows_written_after_the_purge_by_a_late_worker(world):
    cid = connect_and_import(world)
    world.c.post(f"{BASE}/disconnect", headers=world.a.headers)
    with world.sf() as s:  # a page that was already in flight when the store was disconnected
        store.upsert_customers(
            s,
            world.org_a,
            [mapper.map_customer(make_customer(9, email=PII["email"]))],
            now=datetime.now(UTC),
        )
        s.commit()
    assert counts(world.db)["customers"] == 1
    assert lifecycle.finish_pending_purges(world.sf) == 1
    assert PII["email"] not in database_text(world.db)
    assert world.db.get(ShopifyConnection, cid).status == ShopifyConnectionStatus.DISCONNECTED


def test_the_safety_net_removes_an_order_page_that_arrived_after_the_purge(world):
    """Orders are the one resource a late page can leave on its own: nothing else needs to exist."""
    cid = connect_and_import(world)
    world.c.post(f"{BASE}/disconnect", headers=world.a.headers)
    assert counts(world.db) == dict.fromkeys(COMMERCE_NAMES, 0)
    node = make_order(77, customer=None, items=[make_line_item(770, product=None, variant=None)])
    items = node.pop("_items")
    node["lineItems"] = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": items}
    with world.sf() as s:
        store.upsert_orders(s, world.org_a, [mapper.map_order(node)], now=datetime.now(UTC))
        s.commit()
    left = counts(world.db)
    assert (left["orders"], left["order_items"]) == (1, 1)
    assert (left["customers"], left["products"]) == (0, 0)  # orders are all that is left

    assert lifecycle.finish_pending_purges(world.sf) == 1
    assert counts(world.db) == dict.fromkeys(COMMERCE_NAMES, 0)
    assert world.db.get(ShopifyConnection, cid).status == ShopifyConnectionStatus.DISCONNECTED


def test_the_safety_net_never_touches_a_connected_store_or_another_tenant(world):
    cid_a = connect_and_import(world, "a")
    cid_b = connect_and_import_second(world)
    disconnect_without_purge(world, cid_a)
    before_b = counts(world.db, world.org_b)
    assert before_b["customers"] > 0
    assert lifecycle.finish_pending_purges(world.sf) == 1
    assert counts(world.db, world.org_a) == dict.fromkeys(COMMERCE_NAMES, 0)
    assert counts(world.db, world.org_b) == before_b
    assert world.db.get(ShopifyConnection, cid_b).status != ShopifyConnectionStatus.DISCONNECTED


def connect_and_import_second(world):
    """A second, connected organization with its own shop and imported data."""
    world.fake.shop = "beta-shop.myshopify.com"
    cid = seed_connection(world.sf, world.org_b, world.rt, world.fake, shop=world.fake.shop)
    assert full_sync(world.rt, world.sf, cid, world.q) == "completed"
    world.fake.shop = "test-shop.myshopify.com"
    return cid


def test_a_store_that_never_imported_anything_costs_the_safety_net_nothing(world):
    cid = seed_connection(world.sf, world.org_a, world.rt, world.fake)
    disconnect_without_purge(world, cid)
    assert lifecycle.finish_pending_purges(world.sf) == 0


def test_the_ledger_rows_of_a_disconnected_shop_age_out_by_the_normal_retention_window(world):
    connect_and_import(world)
    world.q.jobs.clear()
    deliver(world.c, "products/update", {"id": 1}, webhook_id="kept-then-pruned")
    queued = world.q.jobs.popleft()
    world.c.post(f"{BASE}/disconnect", headers=world.a.headers)

    # the worker gets to the queued event after the disconnect: it is ignored, not applied
    assert webhooks.process_event(world.rt, world.sf, queued.event_id) == "ignored"
    world.db.expire_all()
    assert "kept-then-pruned" in {
        e.webhook_id for e in world.db.scalars(select(ShopifyWebhookEvent))
    }
    assert counts(world.db) == dict.fromkeys(COMMERCE_NAMES, 0)

    with world.sf() as s:  # identifiers only, so the audit trail simply ages out
        s.execute(
            update(ShopifyWebhookEvent).values(received_at=datetime.now(UTC) - timedelta(days=45))
        )
        s.commit()
    assert webhooks.prune_old_events(world.sf, 30) >= 1
    world.db.expire_all()
    remaining = {e.webhook_id for e in world.db.scalars(select(ShopifyWebhookEvent))}
    assert "kept-then-pruned" not in remaining
