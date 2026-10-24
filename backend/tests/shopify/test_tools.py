"""The two engineering tools that support real-world operation: the live Shopify check and the
customers/data_request export. Their logic is proven here against the fake so that, when they run
against a real store, a failure means the store or app is wrong and not the tool."""

import argparse
import json
import os
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select, update

from app.db.models import Customer, ShopifyConnection, ShopifyConnectionStatus
from app.services.integrations.shopify import mapper, store
from tests.helpers import create_org, signup_and_login
from tests.shopify.conftest import seed_connection
from tests.shopify.fake_shopify import (
    make_customer,
    make_line_item,
    make_order,
    make_product,
    make_variant,
)
from tools import customer_data_export, shopify_live_check

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
SECRET_EMAIL = "private-person@example.test"
SECRET_PHONE = "+15550001234"


@pytest.fixture(autouse=True)
def _fresh_tool_state():
    shopify_live_check.FAILURES.clear()


@pytest.fixture()
def org_id(client):
    return uuid.UUID(
        create_org(client, signup_and_login(client, "tools@example.com"), "Tools Co")["id"]
    )


@pytest.fixture()
def cid(session_factory, org_id, runtime, fake):
    return seed_connection(session_factory, org_id, runtime, fake)


def args(**overrides) -> argparse.Namespace:
    values = {"shop": "test-shop.myshopify.com", "pages": 1, "webhooks": False, "refresh": False}
    return argparse.Namespace(**{**values, **overrides})


def run_check(settings, session_factory, runtime, **overrides) -> int:
    return shopify_live_check.run(args(**overrides), settings, session_factory, rt=runtime)


def populate(fake, products=45, customers=10, orders=5):
    fake.products = [
        make_product(n, variants=[make_variant(n * 10 + 1), make_variant(n * 10 + 2)])
        for n in range(1, products + 1)
    ]
    fake.customers = [
        make_customer(n, email=SECRET_EMAIL, phone=SECRET_PHONE) for n in range(1, customers + 1)
    ]
    fake.orders = [
        make_order(n, customer=1, items=[make_line_item(n * 100, product=1, variant=None)])
        for n in range(1, orders + 1)
    ]


# --- the live check -------------------------------------------------------------------------------------


def test_a_healthy_store_passes_and_the_report_has_counts_but_no_pii_and_no_secrets(
    shopify_settings, session_factory, runtime, fake, cid, capsys
):
    populate(fake)
    code = run_check(shopify_settings, session_factory, runtime)
    out = capsys.readouterr().out
    assert code == 0 and shopify_live_check.FAILURES == [], out
    for expected in (
        "found the connection",
        "read the shop",
        "read products   [pages=1 {'objects': 20, 'nested': 40",
        "read customers",
        "read orders",
        "GraphQL cost bucket",
        "granted scopes",
    ):
        assert expected in out, expected
    for secret in (SECRET_EMAIL, SECRET_PHONE, "shpat_", "shprt_", "bf1:", "test-client-secret"):
        assert secret not in out, secret
    assert "with_email': 10" in out  # a COUNT of customers that came back with an email, not values


def test_pagination_is_followed_page_by_page_with_cursors(
    shopify_settings, session_factory, runtime, fake, cid, capsys
):
    populate(fake, products=45)
    run_check(shopify_settings, session_factory, runtime, pages=3)
    out = capsys.readouterr().out
    assert "products page 1   [objects=20 has_next=True" in out
    assert "products page 2   [objects=20 has_next=True" in out
    assert "products page 3   [objects=5 has_next=False" in out
    assert "pages=3 {'objects': 45" in out


def test_unapproved_protected_customer_data_is_a_warning_not_a_failure(
    shopify_settings, session_factory, runtime, fake, cid, capsys
):
    populate(fake)
    fake.restrict_customer_pii = True  # Shopify: nulls plus ACCESS_DENIED for email/phone/name
    code = run_check(shopify_settings, session_factory, runtime)
    out = capsys.readouterr().out
    assert code == 0 and shopify_live_check.FAILURES == []
    assert "WARN  customers   [Shopify withheld protected customer data" in out
    assert "no customer returned an email" in out


def test_a_denied_resource_is_a_warning_and_the_other_resources_are_still_checked(
    shopify_settings, session_factory, runtime, fake, cid, capsys
):
    populate(fake)
    fake.deny = {"orders"}
    code = run_check(shopify_settings, session_factory, runtime)
    out = capsys.readouterr().out
    assert code == 0
    assert "WARN  orders   [Shopify denied access" in out and "read products" in out


def test_an_ungranted_scope_is_reported_as_expected_not_as_a_bug(
    shopify_settings, session_factory, runtime, fake, org_id, capsys
):
    seed_connection(session_factory, org_id, runtime, fake, scopes="read_products")
    populate(fake)
    run_check(shopify_settings, session_factory, runtime)
    out = capsys.readouterr().out
    assert "WARN  customers   [scope read_customers was not granted" in out
    assert "WARN  orders" in out and "read products" in out


def test_a_retired_api_version_that_shopify_fell_forward_is_a_failure(
    shopify_settings, session_factory, runtime, fake, cid, capsys
):
    def older(request):
        response = fake.handler(request)
        response.headers["X-Shopify-API-Version"] = "2025-10"
        return response

    runtime.transport = httpx.MockTransport(older)
    populate(fake)
    assert run_check(shopify_settings, session_factory, runtime) == 1
    out = capsys.readouterr().out
    assert "FAIL  Shopify served the configured API version" in out
    assert "configured=2026-10 served=2025-10" in out


def test_webhook_subscriptions_are_compared_with_what_bizflow_needs(
    shopify_settings, session_factory, runtime, fake, cid, capsys
):
    from app.services.integrations.shopify import webhooks

    populate(fake)
    webhooks.register_webhooks(runtime, session_factory, cid)
    fake.subscriptions[:] = [s for s in fake.subscriptions if s["topic"] != "ORDERS_CREATE"]
    code = run_check(shopify_settings, session_factory, runtime, webhooks=True)
    out = capsys.readouterr().out
    assert code == 1 and "FAIL  all webhook subscriptions exist   [9 of 10]" in out
    assert "missing topics   [ORDERS_CREATE]" in out
    assert "customers/data_request" in out  # the reminder about the console-configured topics


def test_a_forced_refresh_rotates_the_tokens_and_the_new_one_works(
    shopify_settings, session_factory, runtime, fake, cid, capsys
):
    populate(fake)
    code = run_check(shopify_settings, session_factory, runtime, refresh=True)
    out = capsys.readouterr().out
    assert code == 0 and "token refresh rotated and stored both tokens" in out
    assert "the new access token works" in out
    assert any(r["grant_type"] == "refresh_token" for r in fake.token_requests)


@pytest.mark.parametrize(
    ("shop", "expected"),
    [
        ("evil.example.com", "the shop must be <name>.myshopify.com"),
        ("someone-else.myshopify.com", "the store is connected in BizFlow"),
    ],
)
def test_a_bad_or_unconnected_shop_fails_before_any_request_to_shopify(
    shopify_settings, session_factory, runtime, fake, cid, capsys, shop, expected
):
    assert run_check(shopify_settings, session_factory, runtime, shop=shop) == 1
    assert expected in capsys.readouterr().out
    assert fake.requests == []


def test_a_disconnected_store_is_not_checked(
    shopify_settings, session_factory, runtime, fake, cid, capsys
):
    with session_factory() as db:
        db.execute(
            update(ShopifyConnection)
            .where(ShopifyConnection.id == cid)
            .values(status=ShopifyConnectionStatus.DISCONNECTED)
        )
        db.commit()
    assert run_check(shopify_settings, session_factory, runtime) == 1
    assert "the store is connected in BizFlow" in capsys.readouterr().out


def test_without_app_credentials_the_tool_says_so_and_stops(
    shopify_settings, session_factory, runtime, fake, cid, capsys
):
    shopify_settings.shopify_client_id = ""
    assert run_check(shopify_settings, session_factory, runtime) == 1
    assert "Shopify is configured" in capsys.readouterr().out


# --- the data-request export ---------------------------------------------------------------------------------------


@pytest.fixture()
def two_shops(session_factory, client, runtime, fake):
    """Two tenants, each with a connection to its own shop and a customer with the SAME Shopify id."""
    org_a = uuid.UUID(create_org(client, signup_and_login(client, "a-tool@example.com"), "A")["id"])
    org_b = uuid.UUID(create_org(client, signup_and_login(client, "b-tool@example.com"), "B")["id"])
    seed_connection(session_factory, org_a, runtime, fake, shop="shop-a.myshopify.com")
    seed_connection(session_factory, org_b, runtime, fake, shop="shop-b.myshopify.com")

    def customer(email):
        data = mapper.map_customer(make_customer(7, email=email, phone="+15559990000"))
        assert data is not None
        return data

    def order(number, line):
        node = make_order(
            number, customer=7, items=[make_line_item(line, product=None, variant=None)]
        )
        items = node.pop("_items")
        node["lineItems"] = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": items}
        data = mapper.map_order(node)
        assert data is not None
        return data

    with session_factory() as db:
        store.upsert_customers(db, org_a, [customer("alice@a.example")], now=NOW)
        store.upsert_customers(db, org_b, [customer("bob@b.example")], now=NOW)
        db.commit()
        store.upsert_orders(db, org_a, [order(1, 101), order(2, 102)], now=NOW)
        store.upsert_orders(db, org_b, [order(9, 909)], now=NOW)
        db.commit()
    return org_a, org_b


def export(session_factory, tmp_path, *extra, shop="shop-a.myshopify.com", customer="7"):
    out = tmp_path / "request.json"
    code = customer_data_export.main(
        ["--shop", shop, "--customer-id", customer, "--out", str(out), *extra], session_factory
    )
    return code, out


def test_the_export_contains_that_customers_stored_data_and_only_theirs(
    session_factory, two_shops, tmp_path, capsys
):
    org_a, _ = two_shops
    code, out = export(session_factory, tmp_path)
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    record = data["records"][0]
    assert data["shop_domain"] == "shop-a.myshopify.com" and len(data["records"]) == 1
    assert record["organization_id"] == str(org_a)
    assert record["customer"]["email"] == "alice@a.example"
    assert [o["shopify_order_id"] for o in record["orders"]] == [1, 2]
    assert [len(o["items"]) for o in record["orders"]] == [1, 1]
    text = out.read_text(encoding="utf-8")
    assert "bob@b.example" not in text and "909" not in text  # the other tenant's customer 7


def test_the_console_reports_counts_never_the_personal_data(
    session_factory, two_shops, tmp_path, capsys
):
    export(session_factory, tmp_path)
    shown = capsys.readouterr().out
    assert "2 order(s), 2 line item(s)" in shown
    assert "alice@a.example" not in shown and "+15559990000" not in shown


def test_the_file_is_owner_only_and_never_silently_overwritten(
    session_factory, two_shops, tmp_path, capsys
):
    code, out = export(session_factory, tmp_path)
    assert code == 0
    if os.name == "posix":
        assert (out.stat().st_mode & 0o777) == 0o600
    again, _ = export(session_factory, tmp_path)
    assert again == 2 and "already exists" in capsys.readouterr().out
    forced, _ = export(session_factory, tmp_path, "--force")
    assert forced == 0


def test_a_customer_with_nothing_stored_is_a_clear_no_data_answer(
    session_factory, two_shops, tmp_path, capsys
):
    code, out = export(session_factory, tmp_path, customer="999")
    assert code == 1 and not out.exists()
    assert "nothing is stored for that customer" in capsys.readouterr().out


def test_a_shop_with_no_connection_exports_nothing_even_if_the_customer_id_exists_elsewhere(
    session_factory, two_shops, tmp_path
):
    code, out = export(session_factory, tmp_path, shop="unknown-shop.myshopify.com")
    assert code == 1 and not out.exists()


def test_a_redacted_customer_leaves_nothing_personal_to_export(
    session_factory, two_shops, tmp_path
):
    org_a, _ = two_shops
    with session_factory() as db:
        store.anonymize_customer(db, org_a, 7, NOW)
        db.commit()
    code, out = export(session_factory, tmp_path)
    record = json.loads(out.read_text(encoding="utf-8"))["records"][0]["customer"]
    assert code == 0 and record["email"] is None and record["first_name"] is None


def test_the_shop_is_validated_before_any_database_access(session_factory, tmp_path, capsys):
    code, _ = export(session_factory, tmp_path, shop="evil.example.com")
    assert code == 2 and "the shop must be" in capsys.readouterr().out


def test_the_export_only_reads(session_factory, two_shops, tmp_path):
    with session_factory() as db:
        before = db.scalars(select(Customer.updated_at).order_by(Customer.id)).all()
    export(session_factory, tmp_path)
    with session_factory() as db:
        assert db.scalars(select(Customer.updated_at).order_by(Customer.id)).all() == before
