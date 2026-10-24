import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.models import (
    Customer,
    Product,
    ShopifyConnection,
    ShopifyConnectionStatus,
    ShopifySyncState,
    SyncStatus,
)
from app.main import app
from app.services.integrations.shopify import fetch, store
from app.workers.queue import ShopifySyncJob
from tests.helpers import add_member, signup_and_login
from tests.shopify.conftest import connect_through_api, seed_connection
from tests.shopify.fake_shopify import make_customer, make_product

BASE = "/api/integrations/shopify"


@pytest.fixture()
def api(shopify_client, tenants, fake, runtime, session_factory, queue):
    a, b = tenants
    return type(
        "Api",
        (),
        {
            "c": shopify_client,
            "a": a,
            "b": b,
            "fake": fake,
            "rt": runtime,
            "sf": session_factory,
            "q": queue,
        },
    )


def member_headers(api, email, role):
    headers = signup_and_login(api.c, email)
    assert add_member(api.c, api.a.headers, api.a.org["id"], email, role).status_code == 201
    return {**headers, "X-Organization-ID": api.a.org["id"]}


def org_a(api) -> uuid.UUID:
    return uuid.UUID(api.a.org["id"])


def connect_a(api, **kw):
    return seed_connection(api.sf, org_a(api), api.rt, api.fake, **kw)


def import_data(api, org_id, cid=None):
    """Put a product and a customer (with personal data) into an organization."""
    api.fake.products = [make_product(5)]
    api.fake.customers = [
        make_customer(7, email="private-customer@example.com", phone="+15557654321")
    ]
    with api.sf() as s:
        cid = s.scalar(
            select(ShopifyConnection.id).where(ShopifyConnection.organization_id == org_id)
        )
    client = api.rt.authorized_client(api.sf, cid, api.fake.shop)
    product, customer = fetch.fetch_product(client, 5), fetch.fetch_customer(client, 7)
    client.close()
    with api.sf() as s:
        now = datetime.now(UTC)
        store.upsert_products(s, org_id, [product], currency="USD", now=now)
        store.upsert_customers(s, org_id, [customer], now=now)
        s.commit()


# --- status ---------------------------------------------------------------------------------------------


def test_status_requires_login(api):
    assert api.c.get(BASE).status_code == 401


def test_status_without_a_connection(api):
    body = api.c.get(BASE, headers=api.a.headers).json()
    assert body == {
        "available": True,
        "secure_storage": True,
        "webhooks_configured": True,
        "connection": None,
        "scopes": [],
        "sync": [],
        "counts": {"products": 0, "variants": 0, "customers": 0, "orders": 0},
    }


def test_status_reports_when_the_server_is_not_set_up_for_shopify(api, shopify_settings):
    shopify_settings.shopify_client_secret = None
    assert api.c.get(BASE, headers=api.a.headers).json()["available"] is False


def test_every_member_can_read_the_status_but_only_managers_can_change_things(api):
    connect_a(api)
    viewer = member_headers(api, "viewer@example.com", "MEMBER")
    admin = member_headers(api, "admin@example.com", "ADMIN")
    for headers in (viewer, admin):
        assert api.c.get(BASE, headers=headers).status_code == 200
    for method, path, kwargs in (
        ("post", "/connect", {"json": {"shop": api.fake.shop}}),
        ("post", "/sync", {}),
        ("post", "/disconnect", {}),
    ):
        assert getattr(api.c, method)(BASE + path, headers=viewer, **kwargs).status_code == 403, (
            path
        )


def test_status_lists_the_stage_progress_in_order(api):
    cid = connect_a(api)
    api.c.post(BASE + "/sync", headers=api.a.headers)
    body = api.c.get(BASE, headers=api.a.headers).json()
    assert [s["resource"] for s in body["sync"]] == ["PRODUCTS", "CUSTOMERS", "ORDERS"]
    assert all(s["status"] == "PENDING" for s in body["sync"])
    assert body["connection"]["status"] == "SYNCING" and cid


# --- secrets and personal data are never exposed ----------------------------------------------------------------


def test_the_status_response_contains_no_secrets_and_no_customer_data(api):
    connect_a(api)
    import_data(api, org_a(api))
    r = api.c.get(BASE, headers=api.a.headers)
    for needle in (
        "shpat_",
        "shprt_",
        "bf1:",
        "test-client-secret",
        "access_token",
        "refresh_token",
        "encrypted",
        "private-customer@example.com",
        "+15557654321",
        "Lovelace",
        "customer7",
    ):
        assert needle not in r.text, needle
    assert r.json()["counts"]["customers"] == 1  # the number is shown, never the people


def test_no_response_model_has_a_field_that_could_carry_a_secret(api):
    schemas = app.openapi()["components"]["schemas"]
    shopify = {
        name: s
        for name, s in schemas.items()
        if name
        in {
            "ShopifyStatusOut",
            "ConnectionOut",
            "SyncStateOut",
            "CountsOut",
            "AuthorizationOut",
            "CallbackOut",
            "ShopifyConnectIn",
        }
    }
    assert shopify
    fields = {f for s in shopify.values() for f in s.get("properties", {})}
    forbidden = {
        "access_token",
        "refresh_token",
        "access_token_encrypted",
        "refresh_token_encrypted",
        "client_secret",
        "secret",
        "token",
        "email",
        "phone",
        "first_name",
        "last_name",
        "hmac",
        "state",
    }
    assert not (fields & forbidden), fields & forbidden


# --- sync -------------------------------------------------------------------------------------------------------


def test_sync_requires_login_and_a_connection(api):
    assert api.c.post(BASE + "/sync").status_code == 401
    r = api.c.post(BASE + "/sync", headers=api.a.headers)
    assert r.status_code == 404 and "No Shopify store" in r.json()["detail"]


def test_manual_sync_queues_a_job_and_returns_the_new_status(api):
    cid = connect_a(api)
    r = api.c.post(BASE + "/sync", headers=api.a.headers)
    assert r.status_code == 202
    body = r.json()
    assert body["connection"]["status"] == "SYNCING"
    assert [s["status"] for s in body["sync"]] == ["PENDING"] * 3
    assert [type(j) for j in api.q.jobs] == [ShopifySyncJob] and api.q.jobs[0].connection_id == cid


def test_a_sync_already_running_cannot_be_started_again(api):
    connect_a(api)
    assert api.c.post(BASE + "/sync", headers=api.a.headers).status_code == 202
    again = api.c.post(BASE + "/sync", headers=api.a.headers)
    assert again.status_code == 409 and "already in progress" in again.json()["detail"]
    assert len(api.q.jobs) == 1  # one sync, not two


def test_two_simultaneous_requests_start_only_one_sync(api, db):
    """The status flip is one atomic UPDATE: of two racing requests only one can win."""
    cid = connect_a(api)
    with api.sf() as s:
        first = s.execute(
            update(ShopifyConnection)
            .where(
                ShopifyConnection.id == cid,
                ShopifyConnection.status == ShopifyConnectionStatus.CONNECTED,
            )
            .values(status=ShopifyConnectionStatus.SYNCING)
        ).rowcount
        second = s.execute(
            update(ShopifyConnection)
            .where(
                ShopifyConnection.id == cid,
                ShopifyConnection.status == ShopifyConnectionStatus.CONNECTED,
            )
            .values(status=ShopifyConnectionStatus.SYNCING)
        ).rowcount
        s.commit()
    assert (first, second) == (1, 0)


def test_syncing_is_rate_limited_per_store(api, shopify_settings, session_factory):
    shopify_settings.shopify_sync_min_interval_seconds = 60
    cid = connect_a(api)
    with session_factory() as s:
        s.execute(
            update(ShopifyConnection)
            .where(ShopifyConnection.id == cid)
            .values(last_sync_requested_at=datetime.now(UTC) - timedelta(seconds=10))
        )
        s.commit()
    r = api.c.post(BASE + "/sync", headers=api.a.headers)
    assert r.status_code == 429 and 1 <= int(r.headers["Retry-After"]) <= 51
    assert len(api.q.jobs) == 0


def test_a_connection_that_needs_reauthorization_cannot_sync_until_reconnected(api):
    connect_a(api, status=ShopifyConnectionStatus.ERROR)
    r = api.c.post(BASE + "/sync", headers=api.a.headers)
    assert r.status_code == 409 and "reconnect" in r.json()["detail"].lower()


def test_if_the_queue_is_unavailable_the_stages_fail_visibly_instead_of_hanging(api):
    connect_a(api)
    api.q.fail_enqueue = True
    r = api.c.post(BASE + "/sync", headers=api.a.headers)
    body = r.json()
    assert r.status_code == 202 and body["connection"]["status"] == "CONNECTED"
    assert {s["status"] for s in body["sync"]} == {"FAILED"}
    assert (
        body["sync"][0]["error_code"] == "queue_unavailable"
        and "queue" in body["sync"][0]["error_message"]
    )
    api.q.fail_enqueue = False
    assert api.c.post(BASE + "/sync", headers=api.a.headers).status_code == 202  # and a retry works


def test_request_bodies_cannot_inject_fields_into_sync_or_disconnect(api):
    connect_a(api)
    r = api.c.post(
        BASE + "/sync",
        json={"organization_id": api.b.org["id"], "status": "CONNECTED"},
        headers=api.a.headers,
    )
    assert r.status_code == 202  # the body is not an input at all
    with api.sf() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(ShopifyConnection)
                .where(ShopifyConnection.organization_id == uuid.UUID(api.b.org["id"]))
            )
            == 0
        )


# --- disconnect --------------------------------------------------------------------------------------------------


def test_disconnect_requires_login_and_a_connection(api):
    assert api.c.post(BASE + "/disconnect").status_code == 401
    assert api.c.post(BASE + "/disconnect", headers=api.a.headers).status_code == 404


def test_disconnect_wipes_credentials_removes_webhooks_and_purges_the_data(api, db):
    api.fake.subscriptions = []
    cid = connect_a(api)
    from app.services.integrations.shopify.webhooks import register_webhooks

    register_webhooks(api.rt, api.sf, cid)
    assert len(api.fake.subscriptions) == 10
    import_data(api, org_a(api))
    api.c.post(BASE + "/sync", headers=api.a.headers)  # pending stages exist too

    r = api.c.post(BASE + "/disconnect", headers=api.a.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["connection"]["status"] == "DISCONNECTED" and body["sync"] == []
    assert body["counts"] == {"products": 0, "variants": 0, "customers": 0, "orders": 0}
    db.expire_all()
    row = db.scalar(select(ShopifyConnection))
    assert row.access_token_encrypted is None and row.refresh_token_encrypted is None
    assert row.access_token_expires_at is None and row.refresh_token_expires_at is None
    assert db.scalar(select(func.count()).select_from(Product)) == 0
    assert db.scalar(select(func.count()).select_from(Customer)) == 0
    assert db.scalar(select(func.count()).select_from(ShopifySyncState)) == 0
    assert (
        api.fake.subscriptions == []
    )  # BizFlow's webhook subscriptions were deleted at Shopify too


def test_disconnecting_twice_is_a_404_and_a_disconnected_store_cannot_sync(api):
    connect_a(api)
    assert api.c.post(BASE + "/disconnect", headers=api.a.headers).status_code == 200
    assert api.c.post(BASE + "/disconnect", headers=api.a.headers).status_code == 404
    assert api.c.post(BASE + "/sync", headers=api.a.headers).status_code == 404


def test_the_store_can_be_connected_again_after_disconnecting(api):
    api.fake.valid_codes.add("good-code")
    connect_a(api)
    api.c.post(BASE + "/disconnect", headers=api.a.headers)
    r = connect_through_api(api.c, api.a.headers, api.fake)
    assert r.status_code == 200
    assert api.c.get(BASE, headers=api.a.headers).json()["connection"]["status"] == "SYNCING"


def test_disconnect_still_wipes_everything_if_shopify_cannot_be_reached(api, db):
    connect_a(api)
    import_data(api, org_a(api))
    api.fake.revoke_all_tokens()  # webhook removal will fail: it must not block the disconnect
    with api.sf() as s:
        s.execute(update(ShopifyConnection).values(access_token_expires_at=None))
        s.commit()
    assert api.c.post(BASE + "/disconnect", headers=api.a.headers).status_code == 200
    db.expire_all()
    assert db.scalar(select(ShopifyConnection.access_token_encrypted)) is None
    assert db.scalar(select(func.count()).select_from(Product)) == 0


def test_disconnect_works_even_when_secure_storage_was_removed_from_the_server(
    api, shopify_client, db
):
    from app.api.integrations.shopify import get_shopify_runtime

    connect_a(api)
    app.dependency_overrides[get_shopify_runtime] = lambda: None
    assert shopify_client.post(BASE + "/disconnect", headers=api.a.headers).status_code == 200
    db.expire_all()
    assert db.scalar(select(ShopifyConnection.status)) == ShopifyConnectionStatus.DISCONNECTED


# --- tenant isolation ---------------------------------------------------------------------------------------------


def test_one_organization_cannot_see_or_touch_anothers_connection(api, db):
    connect_a(api)
    import_data(api, org_a(api))
    # B sees nothing of A's
    b = api.c.get(BASE, headers=api.b.headers).json()
    assert (
        b["connection"] is None
        and b["sync"] == []
        and b["counts"] == {"products": 0, "variants": 0, "customers": 0, "orders": 0}
    )
    # B cannot act on A's
    assert api.c.post(BASE + "/sync", headers=api.b.headers).status_code == 404
    assert api.c.post(BASE + "/disconnect", headers=api.b.headers).status_code == 404
    # and none of that disturbed A
    a = api.c.get(BASE, headers=api.a.headers).json()
    assert a["connection"]["status"] == "CONNECTED" and a["counts"]["products"] == 1


def test_naming_another_organization_in_the_header_gets_a_404_not_their_data(api):
    connect_a(api)
    import_data(api, org_a(api))
    for path, method in ((BASE, "get"), (BASE + "/sync", "post"), (BASE + "/disconnect", "post")):
        r = getattr(api.c, method)(
            path, headers={**api.b.headers, "X-Organization-ID": api.a.org["id"]}
        )
        assert r.status_code == 404, path
    assert api.c.get(BASE, headers=api.a.headers).json()["connection"]["status"] == "CONNECTED"


def test_each_organization_can_have_its_own_store(api):
    api.fake.valid_codes.update({"good-code", "good-code-2"})
    assert connect_through_api(api.c, api.a.headers, api.fake).status_code == 200
    from tests.shopify.fake_shopify import FakeShopify

    second = FakeShopify(shop="b-store.myshopify.com")
    second.valid_codes = {"good-code"}
    api.rt.transport = second.transport()
    assert (
        connect_through_api(api.c, api.b.headers, second, shop="b-store.myshopify.com").status_code
        == 200
    )
    a = api.c.get(BASE, headers=api.a.headers).json()["connection"]["shop_domain"]
    b = api.c.get(BASE, headers=api.b.headers).json()["connection"]["shop_domain"]
    assert (a, b) == ("test-shop.myshopify.com", "b-store.myshopify.com")


def test_disconnecting_one_organization_never_purges_anothers_data(api, db):
    api.fake.valid_codes.add("good-code")
    connect_a(api)
    import_data(api, org_a(api))
    cid_b = seed_connection(
        api.sf, uuid.UUID(api.b.org["id"]), api.rt, api.fake, shop="b-store.myshopify.com"
    )
    import_data(api, uuid.UUID(api.b.org["id"]))
    assert api.c.post(BASE + "/disconnect", headers=api.b.headers).status_code == 200
    db.expire_all()
    assert (
        db.scalar(
            select(func.count()).select_from(Product).where(Product.organization_id == org_a(api))
        )
        == 1
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(Product)
            .where(Product.organization_id == uuid.UUID(api.b.org["id"]))
        )
        == 0
    )
    assert cid_b


def test_api_responses_never_contain_ids_that_enable_enumeration(api):
    connect_a(api)
    body = json.dumps(api.c.get(BASE, headers=api.a.headers).json())
    assert not any(token in body for token in ("organization_id", "connection_id", '"id"'))


def test_the_sync_state_rows_are_organization_scoped_by_foreign_key(api, db):
    connect_a(api)
    api.c.post(BASE + "/sync", headers=api.a.headers)
    rows = db.scalars(select(ShopifySyncState)).all()
    assert rows and {r.organization_id for r in rows} == {org_a(api)}
    assert {r.status for r in rows} == {SyncStatus.PENDING}
