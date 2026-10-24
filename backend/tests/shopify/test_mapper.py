from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.integrations.shopify import mapper
from tests.shopify.fake_shopify import (
    make_customer,
    make_line_item,
    make_order,
    make_product,
    make_variant,
)


def product_node(**kw):
    """A product exactly as the GraphQL API returns it (nested variants connection)."""
    p = make_product(**kw)
    variants = p.pop("_variants")
    p["variants"] = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": variants}
    return p


def order_node(**kw):
    o = make_order(**kw)
    items = o.pop("_items")
    o["lineItems"] = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": items}
    return o


# --- products and variants -------------------------------------------------------------------------


def test_product_maps_every_field():
    p = mapper.map_product(product_node(number=7, title="Blue Mug", tags=["a", "b"]))
    assert p is not None
    assert p.shopify_product_id == 7
    assert (p.title, p.handle, p.vendor, p.product_type) == (
        "Blue Mug",
        "product-7",
        "Acme",
        "Gadget",
    )
    assert p.description == "Description of product 7" and p.status == "ACTIVE"
    assert p.tags == ["a", "b"]
    assert p.product_url == "https://shop.example/products/product-7"
    assert p.shopify_created_at == datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    assert p.shopify_updated_at == datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    assert p.variants_complete and p.variants_cursor is None and p.malformed_variants == 0


def test_variant_maps_money_inventory_and_availability():
    node = make_variant(
        701,
        title="Large",
        price="24.50",
        compare_at="30.00",
        sku="MUG-L",
        inventory=12,
        available=False,
    )
    v = mapper.map_variant(node)
    assert v is not None
    assert v.shopify_variant_id == 701 and v.title == "Large" and v.sku == "MUG-L"
    assert v.price == Decimal("24.50") and v.compare_at_price == Decimal("30.00")
    assert v.inventory_quantity == 12 and v.available is False


def test_variant_tolerates_missing_and_null_fields():
    v = mapper.map_variant({"id": "gid://shopify/ProductVariant/5"})
    assert v is not None
    assert (v.title, v.sku, v.price, v.compare_at_price, v.inventory_quantity) == (
        "Default",
        None,
        None,
        None,
        None,
    )
    assert v.available is True  # unknown availability is not "sold out"
    nulls = mapper.map_variant(
        {
            "id": "gid://shopify/ProductVariant/6",
            "price": None,
            "inventoryQuantity": None,
            "sku": "",
        }
    )
    assert nulls is not None and nulls.price is None and nulls.sku is None


def test_inventory_is_unknown_not_zero_when_the_store_does_not_report_it():
    untracked = mapper.map_variant(make_variant(8, inventory=None))
    assert untracked is not None and untracked.inventory_quantity is None
    oversold = mapper.map_variant(make_variant(9, inventory=-3))
    assert oversold is not None and oversold.inventory_quantity == -3


@pytest.mark.parametrize(
    "node",
    [
        None,
        "x",
        [],
        {},
        {"id": None},
        {"id": "gid://shopify/Customer/1", "title": "wrong type"},
        {"id": "gid://shopify/Product/abc", "title": "bad id"},
        {"id": "gid://shopify/Product/1"},  # no title: cannot name it
        {"id": "gid://shopify/Product/1", "title": "   "},
    ],
)
def test_unidentifiable_products_are_skipped_not_fatal(node):
    assert mapper.map_product(node) is None


def test_malformed_variants_are_counted_and_do_not_discard_the_product():
    node = product_node(number=3)
    node["variants"]["nodes"] = [make_variant(301), {"id": "junk"}, None, make_variant(302)]
    p = mapper.map_product(node)
    assert p is not None
    assert [v.shopify_variant_id for v in p.variants] == [301, 302]
    assert p.malformed_variants == 2


def test_a_product_with_more_variants_than_one_page_says_so():
    node = product_node(number=4)
    node["variants"]["pageInfo"] = {"hasNextPage": True, "endCursor": "abc"}
    p = mapper.map_product(node)
    assert p is not None and p.variants_complete is False and p.variants_cursor == "abc"


def test_product_defaults_and_text_cleaning():
    node = product_node(number=5)
    node.update(status=None, tags="not-a-list", description="line1\x00\x07\nline2  ", vendor="  ")
    p = mapper.map_product(node)
    assert p is not None
    assert p.status == "ACTIVE" and p.tags == [] and p.vendor is None
    assert p.description == "line1\nline2"  # control characters stripped


def test_unknown_status_values_survive_uppercased_so_new_shopify_states_do_not_break_a_sync():
    node = product_node(number=6)
    node["status"] = "scheduled"
    p = mapper.map_product(node)
    assert p is not None and p.status == "SCHEDULED"


def test_non_web_product_urls_are_dropped():
    node = product_node(number=8)
    for bad in ("javascript:alert(1)", "ftp://x/y", "/relative", "data:text/html,<b>"):
        node["onlineStoreUrl"] = bad
        p = mapper.map_product(node)
        assert p is not None and p.product_url is None
    node["onlineStoreUrl"] = "https://ok.example/p"
    assert mapper.map_product(node).product_url == "https://ok.example/p"


def test_tags_are_bounded():
    node = product_node(number=9)
    node["tags"] = [f"t{i}" for i in range(1000)]
    assert len(mapper.map_product(node).tags) == mapper.MAX_TAGS


# --- customers ---------------------------------------------------------------------------------------


def test_customer_maps_contact_details_and_marketing_consent():
    c = mapper.map_customer(make_customer(11, email="ada@example.com", phone="+15551234567"))
    assert c is not None
    assert c.shopify_customer_id == 11
    assert (c.email, c.phone, c.first_name, c.last_name) == (
        "ada@example.com",
        "+15551234567",
        "Ada",
        "Lovelace",
    )
    assert c.accepts_marketing is True


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("SUBSCRIBED", True),
        ("subscribed", True),
        ("NOT_SUBSCRIBED", False),
        ("UNSUBSCRIBED", False),
        ("PENDING", False),
        ("INVALID", False),
        ("REDACTED", False),
        (None, None),
        (5, None),
    ],
)
def test_marketing_consent_is_unknown_when_not_reported(state, expected):
    node = make_customer(12)
    node["defaultEmailAddress"]["marketingState"] = state
    assert mapper.map_customer(node).accepts_marketing is expected


def test_customers_without_email_or_phone_are_valid():
    node = make_customer(13)
    node["defaultEmailAddress"] = None
    node["defaultPhoneNumber"] = None
    c = mapper.map_customer(node)
    assert c is not None and c.email is None and c.phone is None and c.accepts_marketing is None


def test_customers_whose_personal_data_is_withheld_are_still_imported_by_id():
    """Without protected-customer-data approval Shopify returns the customer with fields nulled."""
    node = make_customer(14)
    node.update(firstName=None, lastName=None, defaultEmailAddress=None, defaultPhoneNumber=None)
    c = mapper.map_customer(node)
    assert c is not None and c.shopify_customer_id == 14
    assert (c.email, c.phone, c.first_name, c.last_name, c.accepts_marketing) == (None,) * 5


@pytest.mark.parametrize("node", [None, {}, {"id": "gid://shopify/Product/1"}, {"id": "bad"}, "x"])
def test_unidentifiable_customers_are_skipped(node):
    assert mapper.map_customer(node) is None


def test_overlong_contact_fields_are_truncated_to_the_column_sizes():
    node = make_customer(15, email="a" * 400 + "@x.com", phone="9" * 100)
    c = mapper.map_customer(node)
    assert len(c.email) == 320 and len(c.phone) == 50


# --- orders ------------------------------------------------------------------------------------------


def test_order_maps_totals_status_customer_and_line_items():
    items = [make_line_item(1, product=7, variant=701, quantity=2, price="10.00")]
    o = mapper.map_order(order_node(number=21, customer=11, items=items, total="23.50"))
    assert o is not None
    assert o.shopify_order_id == 21 and o.order_number == "#1021"
    assert o.shopify_customer_id == 11
    assert (o.financial_status, o.fulfillment_status) == ("PAID", "UNFULFILLED")
    assert (o.currency, o.subtotal, o.total) == ("USD", Decimal("35.00"), Decimal("23.50"))
    assert o.cancelled_at is None and o.items_complete
    (item,) = o.line_items
    assert (item.shopify_line_item_id, item.shopify_product_id, item.shopify_variant_id) == (
        1,
        7,
        701,
    )
    assert (item.quantity, item.price, item.title) == (2, Decimal("10.00"), "Item 1")


def test_cancelled_orders_keep_their_cancellation_time():
    o = mapper.map_order(order_node(number=22, cancelled_at="2026-10-03T09:30:00Z"))
    assert o.cancelled_at == datetime(2026, 10, 3, 9, 30, tzinfo=UTC)


def test_guest_orders_and_deleted_products_map_without_references():
    items = [make_line_item(2, product=None, variant=None)]
    o = mapper.map_order(order_node(number=23, customer=None, items=items))
    assert o is not None and o.shopify_customer_id is None
    assert o.line_items[0].shopify_product_id is None and o.line_items[0].shopify_variant_id is None


def test_order_tolerates_missing_totals_and_statuses():
    o = mapper.map_order({"id": "gid://shopify/Order/24"})
    assert o is not None
    assert (o.order_number, o.currency, o.subtotal, o.total, o.financial_status) == (None,) * 5
    assert o.line_items == [] and o.items_complete


def test_malformed_line_items_are_counted_and_skipped():
    node = order_node(number=25)
    node["lineItems"]["nodes"] = [
        make_line_item(5, product=1, variant=2),
        {"id": "gid://shopify/LineItem/6", "quantity": "many"},
        {"id": "nope", "quantity": 1},
        {"id": "gid://shopify/LineItem/7", "quantity": -1},
        None,
    ]
    o = mapper.map_order(node)
    assert [i.shopify_line_item_id for i in o.line_items] == [5]
    assert o.malformed_items == 4


def test_an_order_with_more_line_items_than_one_page_says_so():
    node = order_node(number=26)
    node["lineItems"]["pageInfo"] = {"hasNextPage": True, "endCursor": "zzz"}
    o = mapper.map_order(node)
    assert o.items_complete is False and o.items_cursor == "zzz"


@pytest.mark.parametrize("node", [None, {}, {"id": "gid://shopify/Product/1"}, "x", 5])
def test_unidentifiable_orders_are_skipped(node):
    assert mapper.map_order(node) is None


# --- scalar helpers ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("12.50", Decimal("12.50")),
        ("0", Decimal("0")),
        (" 3.1 ", Decimal("3.1")),
        ("-4.20", Decimal("-4.20")),
        (12, Decimal("12")),
        (None, None),
        ("", None),
        ("abc", None),
        ("NaN", None),
        ("Infinity", None),
        ("-Infinity", None),
        ("1e30", None),  # absurd magnitude
        (True, None),
        ([1], None),
        ({"a": 1}, None),
    ],
)
def test_money_parsing(value, expected):
    assert mapper.money(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-21T10:00:00Z", datetime(2026, 10, 21, 10, 0, tzinfo=UTC)),
        ("2026-10-21T10:00:00+02:00", datetime(2026, 10, 21, 8, 0, tzinfo=UTC)),
        ("2026-10-21T10:00:00", datetime(2026, 10, 21, 10, 0, tzinfo=UTC)),
        (None, None),
        ("", None),
        ("yesterday", None),
        (1700000000, None),
    ],
)
def test_timestamp_parsing_always_yields_aware_utc_or_none(value, expected):
    assert mapper.timestamp(value) == expected


def test_integer_parsing():
    assert mapper.integer(5) == 5 and mapper.integer("7") == 7 and mapper.integer("-2") == -2
    for bad in (True, False, None, "x", 1.5, "9" * 30):
        assert mapper.integer(bad) is None


def test_mappers_are_pure_and_deterministic():
    node = product_node(number=40)
    assert mapper.map_product(node) == mapper.map_product(node)
    original = repr(node)
    mapper.map_product(node)
    assert repr(node) == original  # the input is never modified
