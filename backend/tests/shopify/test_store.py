import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.models import Customer, Order, OrderItem, Organization, Product, ProductVariant
from app.services.commerce import queries
from app.services.integrations.shopify import mapper, store
from tests.shopify.fake_shopify import (
    make_customer,
    make_line_item,
    make_order,
    make_product,
    make_variant,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def new_org(db, slug) -> uuid.UUID:
    org = Organization(name=slug.title(), slug=slug)
    db.add(org)
    db.commit()
    return org.id


@pytest.fixture()
def org(db):
    return new_org(db, "alpha")


@pytest.fixture()
def other(db):
    return new_org(db, "beta")


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


def count(db, model, **where) -> int:
    stmt = select(func.count()).select_from(model)
    for key, value in where.items():
        stmt = stmt.where(getattr(model, key) == value)
    return int(db.scalar(stmt))


def write_products(db, org, products, now=NOW):
    result = store.upsert_products(db, org, products, currency="USD", now=now)
    db.commit()
    return result


# --- products and variants ---------------------------------------------------------------------------


def test_first_write_creates_the_product_and_its_variants(db, org):
    result = write_products(
        db, org, [product(1, variants=[make_variant(101), make_variant(102, price="5.00")])]
    )
    assert (result.written, result.stale) == (1, 0)
    p = db.scalar(select(Product))
    assert (p.organization_id, p.shopify_product_id, p.title, p.currency) == (
        org,
        1,
        "Product 1",
        "USD",
    )
    assert p.tags == ["new", "sale"] and p.deleted_at is None and p.synced_at is not None
    assert isinstance(p.id, uuid.UUID) and p.id != uuid.UUID(
        int=1
    )  # BizFlow owns its own primary key
    variants = db.scalars(select(ProductVariant).order_by(ProductVariant.shopify_variant_id)).all()
    assert [v.shopify_variant_id for v in variants] == [101, 102]
    assert variants[1].price == Decimal("5.0000") and variants[0].product_id == p.id


def test_syncing_the_same_data_again_never_duplicates_anything(db, org):
    data = [
        product(n, variants=[make_variant(n * 10 + 1), make_variant(n * 10 + 2)])
        for n in range(1, 6)
    ]
    for _ in range(3):
        write_products(db, org, data)
    assert count(db, Product) == 5 and count(db, ProductVariant) == 10


def test_a_changed_product_is_updated_in_place_keeping_its_id(db, org):
    write_products(db, org, [product(1, title="Old name")])
    before = db.scalar(select(Product))
    original_id = before.id
    write_products(
        db,
        org,
        [product(1, title="New name", status="ARCHIVED", updated="2026-10-05T10:00:00Z")],
        now=NOW + timedelta(hours=1),
    )
    db.expire_all()
    after = db.scalar(select(Product))
    assert (after.id, after.title, after.status) == (original_id, "New name", "ARCHIVED")
    assert count(db, Product) == 1


def test_a_page_with_the_same_product_twice_does_not_break_the_upsert(db, org):
    write_products(db, org, [product(1, title="first"), product(1, title="second")])
    assert count(db, Product) == 1


def test_variants_removed_in_shopify_are_marked_deleted_when_the_list_is_complete(db, org):
    write_products(
        db, org, [product(1, variants=[make_variant(101), make_variant(102), make_variant(103)])]
    )
    write_products(
        db,
        org,
        [product(1, variants=[make_variant(101)], updated="2026-10-06T10:00:00Z")],
        now=NOW + timedelta(hours=1),
    )
    live = db.scalars(
        select(ProductVariant.shopify_variant_id).where(ProductVariant.deleted_at.is_(None))
    ).all()
    assert live == [101]
    assert count(db, ProductVariant) == 3  # history kept, only marked


def test_an_incomplete_variant_list_never_deletes_anything(db, org):
    write_products(db, org, [product(1, variants=[make_variant(101), make_variant(102)])])
    partial = product(1, variants=[make_variant(101)], updated="2026-10-06T10:00:00Z")
    partial = mapper.ProductData(**{**partial.__dict__, "variants_complete": False})
    write_products(db, org, [partial], now=NOW + timedelta(hours=1))
    assert count(db, ProductVariant, deleted_at=None) == 2


def test_a_product_with_no_variants_marks_all_its_old_variants_deleted(db, org):
    write_products(db, org, [product(1, variants=[make_variant(101)])])
    write_products(
        db,
        org,
        [product(1, variants=[], updated="2026-10-06T10:00:00Z")],
        now=NOW + timedelta(hours=1),
    )
    assert count(db, ProductVariant, deleted_at=None) == 0


def test_an_older_version_never_overwrites_a_newer_one(db, org):
    write_products(db, org, [product(1, title="Newest", updated="2026-10-10T10:00:00Z")])
    result = write_products(
        db,
        org,
        [product(1, title="Stale", updated="2026-10-01T10:00:00Z", variants=[make_variant(999)])],
        now=NOW + timedelta(hours=1),
    )
    assert (result.written, result.stale) == (0, 1)
    p = db.scalar(select(Product))
    assert p.title == "Newest"
    assert (
        count(db, ProductVariant, shopify_variant_id=999) == 0
    )  # the stale variants were not written
    assert p.synced_at.replace(tzinfo=UTC) == NOW + timedelta(
        hours=1
    )  # but it was seen, so not "unseen"


def test_equal_timestamps_are_applied(db, org):
    write_products(db, org, [product(1, title="A", updated="2026-10-10T10:00:00Z")])
    write_products(db, org, [product(1, title="B", updated="2026-10-10T10:00:00Z")])
    assert db.scalar(select(Product.title)) == "B"


def test_the_same_shopify_id_in_two_organizations_is_two_independent_rows(db, org, other):
    write_products(db, org, [product(1, title="Alpha's")])
    write_products(db, other, [product(1, title="Beta's")])
    titles = {p.organization_id: p.title for p in db.scalars(select(Product))}
    assert titles == {org: "Alpha's", other: "Beta's"}
    write_products(db, org, [product(1, title="Alpha v2", updated="2026-10-07T10:00:00Z")])
    assert db.scalar(select(Product.title).where(Product.organization_id == other)) == "Beta's"


def test_a_deleted_product_comes_back_when_shopify_returns_it(db, org):
    write_products(db, org, [product(1)])
    assert store.mark_product_deleted(db, org, 1, NOW) is True
    db.commit()
    assert db.scalar(select(Product.deleted_at)) is not None
    write_products(
        db, org, [product(1, updated="2026-10-09T10:00:00Z")], now=NOW + timedelta(days=1)
    )
    db.expire_all()
    assert db.scalar(select(Product.deleted_at)) is None


def test_deleting_a_product_keeps_the_row_and_deactivates_its_variants(db, org, other):
    write_products(db, org, [product(1, variants=[make_variant(101)])])
    write_products(db, other, [product(1, variants=[make_variant(201)])])
    assert store.mark_product_deleted(db, org, 1, NOW)
    db.commit()
    assert count(db, Product) == 2
    live = {
        (p.organization_id) for p in db.scalars(select(Product).where(Product.deleted_at.is_(None)))
    }
    assert live == {other}
    assert count(db, ProductVariant, organization_id=org, deleted_at=None) == 0
    assert count(db, ProductVariant, organization_id=other, deleted_at=None) == 1
    assert store.mark_product_deleted(db, org, 999, NOW) is False


# --- customers -----------------------------------------------------------------------------------------


def write_customers(db, org, customers, now=NOW):
    result = store.upsert_customers(db, org, customers, now=now)
    db.commit()
    return result


def test_customers_upsert_idempotently(db, org):
    data = [customer(n) for n in range(1, 4)]
    for _ in range(2):
        write_customers(db, org, data)
    assert count(db, Customer) == 3
    c = db.scalar(select(Customer).where(Customer.shopify_customer_id == 2))
    assert (c.email, c.first_name, c.accepts_marketing) == ("customer2@example.com", "Ada", True)


def test_a_customer_with_no_email_or_phone_is_stored(db, org):
    node = make_customer(5)
    node["defaultEmailAddress"] = None
    node["defaultPhoneNumber"] = None
    write_customers(db, org, [mapper.map_customer(node)])
    c = db.scalar(select(Customer))
    assert c.email is None and c.phone is None and c.shopify_customer_id == 5


def test_withdrawn_access_to_personal_data_is_reflected_not_kept_stale(db, org):
    write_customers(db, org, [customer(1)])
    withheld = make_customer(1, updated="2026-10-09T10:00:00Z")
    withheld.update(firstName=None, defaultEmailAddress=None, defaultPhoneNumber=None)
    write_customers(db, org, [mapper.map_customer(withheld)], now=NOW + timedelta(hours=1))
    c = db.scalar(select(Customer))
    assert (c.email, c.phone, c.first_name) == (None, None, None)


def test_a_stale_customer_version_is_ignored(db, org):
    write_customers(db, org, [customer(1, first="Grace", updated="2026-10-10T10:00:00Z")])
    result = write_customers(db, org, [customer(1, first="Old", updated="2026-10-01T10:00:00Z")])
    assert result.stale == 1 and db.scalar(select(Customer.first_name)) == "Grace"


def test_customer_deletion_is_soft_and_redaction_erases_personal_data(db, org):
    write_customers(db, org, [customer(1), customer(2)])
    assert store.mark_customer_deleted(db, org, 1, NOW)
    assert store.anonymize_customer(db, org, 2, NOW)
    db.commit()
    rows = {c.shopify_customer_id: c for c in db.scalars(select(Customer))}
    assert (
        rows[1].deleted_at is not None and rows[1].email is not None
    )  # deletion alone keeps the data
    assert rows[2].deleted_at is not None
    assert (
        rows[2].email,
        rows[2].phone,
        rows[2].first_name,
        rows[2].last_name,
        rows[2].accepts_marketing,
    ) == (None,) * 5
    assert rows[2].shopify_customer_id == 2  # the row survives so orders keep their reference
    assert store.anonymize_customer(db, org, 404, NOW) is False


def test_redaction_only_touches_the_named_customer_in_the_named_organization(db, org, other):
    write_customers(db, org, [customer(1)])
    write_customers(db, other, [customer(1, email="other@example.com")])
    store.anonymize_customer(db, org, 1, NOW)
    db.commit()
    kept = db.scalar(select(Customer).where(Customer.organization_id == other))
    assert kept.email == "other@example.com" and kept.deleted_at is None


# --- orders --------------------------------------------------------------------------------------------


def write_orders(db, org, orders, now=NOW):
    result = store.upsert_orders(db, org, orders, now=now)
    db.commit()
    return result


def test_orders_link_to_their_customer_products_and_variants(db, org):
    write_products(db, org, [product(7, variants=[make_variant(701)])])
    write_customers(db, org, [customer(11)])
    items = [
        make_line_item(1, product=7, variant=701, quantity=2),
        make_line_item(2, product=None, variant=None),
    ]
    write_orders(db, org, [order(21, customer=11, items=items)])
    o = db.scalar(select(Order))
    c = db.scalar(select(Customer))
    p = db.scalar(select(Product))
    v = db.scalar(select(ProductVariant))
    assert o.customer_id == c.id and o.order_number == "#1021" and o.total == Decimal("39.9800")
    linked, free = db.scalars(select(OrderItem).order_by(OrderItem.shopify_line_item_id)).all()
    assert (linked.product_id, linked.variant_id, linked.quantity) == (p.id, v.id, 2)
    assert (free.product_id, free.variant_id) == (None, None)
    assert (linked.shopify_product_id, linked.shopify_variant_id) == (
        7,
        701,
    )  # kept for later linking


def test_an_order_for_an_unknown_customer_or_product_is_still_stored(db, org):
    write_orders(
        db, org, [order(21, customer=999, items=[make_line_item(1, product=888, variant=777)])]
    )
    o = db.scalar(select(Order))
    i = db.scalar(select(OrderItem))
    assert o.customer_id is None
    assert (i.product_id, i.variant_id, i.shopify_product_id) == (None, None, 888)


def test_orders_and_items_upsert_without_duplicates(db, org):
    data = [
        order(
            n,
            items=[
                make_line_item(n * 10 + 1, product=1, variant=1),
                make_line_item(n * 10 + 2, product=1, variant=1),
            ],
        )
        for n in range(1, 4)
    ]
    for _ in range(3):
        write_orders(db, org, data)
    assert count(db, Order) == 3 and count(db, OrderItem) == 6


def test_cancelling_an_order_updates_it_and_keeps_its_history(db, org):
    write_orders(db, org, [order(21, items=[make_line_item(1, product=1, variant=1)])])
    write_orders(
        db,
        org,
        [
            order(
                21,
                cancelled_at="2026-10-03T09:30:00Z",
                updated="2026-10-03T09:30:00Z",
                items=[make_line_item(1, product=1, variant=1)],
            )
        ],
    )
    o = db.scalar(select(Order))
    assert o.cancelled_at is not None and count(db, OrderItem) == 1


def test_line_items_removed_by_an_order_edit_are_removed_locally(db, org):
    write_orders(
        db,
        org,
        [
            order(
                21,
                items=[
                    make_line_item(1, product=1, variant=1),
                    make_line_item(2, product=1, variant=1),
                ],
            )
        ],
    )
    write_orders(
        db,
        org,
        [
            order(
                21, updated="2026-10-04T10:00:00Z", items=[make_line_item(2, product=1, variant=1)]
            )
        ],
    )
    assert db.scalars(select(OrderItem.shopify_line_item_id)).all() == [2]


def test_an_incomplete_item_list_never_deletes_items(db, org):
    write_orders(
        db,
        org,
        [
            order(
                21,
                items=[
                    make_line_item(1, product=1, variant=1),
                    make_line_item(2, product=1, variant=1),
                ],
            )
        ],
    )
    partial = order(
        21, updated="2026-10-04T10:00:00Z", items=[make_line_item(1, product=1, variant=1)]
    )
    partial = mapper.OrderData(**{**partial.__dict__, "items_complete": False})
    write_orders(db, org, [partial])
    assert count(db, OrderItem) == 2


def test_a_stale_order_version_is_ignored(db, org):
    write_orders(db, org, [order(21, total="50.00", updated="2026-10-10T10:00:00Z")])
    result = write_orders(db, org, [order(21, total="1.00", updated="2026-10-01T10:00:00Z")])
    assert result.stale == 1 and db.scalar(select(Order.total)) == Decimal("50.0000")


def test_orders_in_different_organizations_stay_separate(db, org, other):
    write_orders(db, org, [order(21, items=[make_line_item(1, product=1, variant=1)])])
    write_orders(
        db, other, [order(21, total="9.00", items=[make_line_item(1, product=1, variant=1)])]
    )
    assert count(db, Order) == 2 and count(db, OrderItem) == 2
    assert {o.organization_id: o.total for o in db.scalars(select(Order))} == {
        org: Decimal("39.9800"),
        other: Decimal("9.0000"),
    }


# --- the database itself refuses cross-tenant references -----------------------------------------------------


def test_a_variant_cannot_point_at_another_organizations_product(db, org, other):
    write_products(db, org, [product(1)])
    foreign_product = db.scalar(select(Product.id))
    db.add(
        ProductVariant(
            organization_id=other,  # a different tenant than the product's
            product_id=foreign_product,
            shopify_variant_id=555,
            title="Smuggled",
            synced_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_an_order_cannot_reference_another_organizations_customer(db, org, other):
    write_customers(db, org, [customer(1)])
    foreign = db.scalar(select(Customer.id))
    db.add(Order(organization_id=other, shopify_order_id=1, customer_id=foreign, synced_at=NOW))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_an_order_item_cannot_reference_another_organizations_order(db, org, other):
    write_orders(db, org, [order(1)])
    foreign = db.scalar(select(Order.id))
    db.add(
        OrderItem(
            organization_id=other, order_id=foreign, shopify_line_item_id=1, title="x", quantity=1
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_shopify_ids_are_unique_per_organization_at_the_database_level(db, org):
    db.add_all(
        [
            Product(organization_id=org, shopify_product_id=1, title="a", synced_at=NOW),
            Product(organization_id=org, shopify_product_id=1, title="b", synced_at=NOW),
        ]
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# --- reconciliation, counts, purge ---------------------------------------------------------------------------


def test_unseen_rows_are_marked_deleted_after_a_complete_import_and_only_those(db, org, other):
    run_start = NOW + timedelta(hours=1)
    write_products(db, org, [product(1), product(2)], now=NOW)  # older run
    write_products(
        db, org, [product(1, updated="2026-10-08T10:00:00Z")], now=run_start + timedelta(minutes=1)
    )
    write_products(db, other, [product(1)], now=NOW)  # another tenant, equally old
    write_customers(db, org, [customer(1), customer(2)], now=NOW)
    write_customers(
        db, org, [customer(1, updated="2026-10-08T10:00:00Z")], now=run_start + timedelta(minutes=1)
    )
    removed = store.soft_delete_unseen(db, org, products=True, customers=True, since=run_start)
    db.commit()
    assert removed == {"products": 1, "customers": 1}
    live = {
        (p.organization_id, p.shopify_product_id)
        for p in db.scalars(select(Product).where(Product.deleted_at.is_(None)))
    }
    assert live == {
        (org, 1),
        (other, 1),
    }  # product 2 is gone for org; the other tenant is untouched
    assert {
        c.shopify_customer_id
        for c in db.scalars(select(Customer).where(Customer.deleted_at.is_(None)))
    } == {1}


def test_reconciliation_can_be_limited_to_one_resource(db, org):
    run_start = NOW + timedelta(hours=1)
    write_products(db, org, [product(1)], now=NOW)
    write_customers(db, org, [customer(1)], now=NOW)
    assert store.soft_delete_unseen(db, org, products=True, customers=False, since=run_start) == {
        "products": 1,
        "customers": 0,
    }
    db.commit()
    assert count(db, Customer, deleted_at=None) == 1


def test_counts_exclude_deleted_rows_and_other_organizations(db, org, other):
    write_products(
        db, org, [product(1, variants=[make_variant(101), make_variant(102)]), product(2)]
    )
    write_products(db, other, [product(1)])
    write_customers(db, org, [customer(1), customer(2)])
    write_orders(db, org, [order(1), order(2), order(3)])
    store.mark_product_deleted(db, org, 2, NOW)
    store.mark_customer_deleted(db, org, 2, NOW)
    db.commit()
    c = store.commerce_counts(db, org)
    assert (c.products, c.variants, c.customers, c.orders) == (1, 2, 1, 3)
    assert store.commerce_counts(db, other).products == 1


def test_purge_removes_only_this_organizations_data_children_first(db, org, other):
    for who in (org, other):
        write_products(db, who, [product(1, variants=[make_variant(10 + (who == org))])])
        write_customers(db, who, [customer(1)])
        write_orders(
            db, who, [order(1, customer=1, items=[make_line_item(1, product=1, variant=None)])]
        )
    removed = store.purge_shop_data(db, org, batch=1)  # a tiny batch forces the loop to repeat
    assert removed == {
        "order_items": 1,
        "orders": 1,
        "customers": 1,
        "product_variants": 1,
        "products": 1,
    }
    for model in (Product, ProductVariant, Customer, Order, OrderItem):
        assert count(db, model, organization_id=org) == 0
        assert count(db, model, organization_id=other) == 1
    assert store.purge_shop_data(db, org) == dict.fromkeys(removed, 0)  # idempotent


# --- the read service other phases use ---------------------------------------------------------------------------


def test_the_read_service_never_crosses_organizations(db, org, other):
    write_products(
        db, org, [product(1, title="Alpha Hoodie", variants=[make_variant(101, price="40.00")])]
    )
    write_products(db, other, [product(1, title="Beta Secret Hoodie")])
    write_customers(db, org, [customer(1)])
    write_customers(db, other, [customer(1, email="beta@example.com")])
    write_orders(db, org, [order(1, customer=1, items=[make_line_item(1, product=1, variant=101)])])
    write_orders(db, other, [order(1)])

    mine = queries.list_products(db, org)
    theirs = queries.list_products(db, other)
    assert [p.title for p in mine] == ["Alpha Hoodie"] and [p.title for p in theirs] == [
        "Beta Secret Hoodie"
    ]
    foreign_product = theirs[0].id
    foreign_customer = db.scalar(select(Customer.id).where(Customer.organization_id == other))
    foreign_order = db.scalar(select(Order.id).where(Order.organization_id == other))
    # a guessed or leaked id from another tenant behaves exactly like one that does not exist
    assert queries.get_product(db, org, foreign_product) is None
    assert queries.list_variants(db, org, foreign_product) == []
    assert queries.get_customer(db, org, foreign_customer) is None
    assert queries.get_order(db, org, foreign_order) is None
    assert queries.list_order_items(db, org, foreign_order) == []
    assert queries.orders_for_customer(db, org, foreign_customer) == []
    # and the same calls with the right organization work
    assert queries.get_product(db, other, foreign_product) is not None


def test_the_read_service_hides_deleted_and_inactive_products_and_escapes_search(db, org):
    write_products(
        db,
        org,
        [
            product(1, title="100% Cotton Tee"),
            product(2, title="Draft thing", status="DRAFT"),
            product(3, title="Gone"),
        ],
    )
    store.mark_product_deleted(db, org, 3, NOW)
    db.commit()
    assert [p.title for p in queries.list_products(db, org)] == ["100% Cotton Tee"]
    assert {p.title for p in queries.list_products(db, org, status=None)} == {
        "100% Cotton Tee",
        "Draft thing",
    }
    assert [p.title for p in queries.list_products(db, org, search="100%")] == ["100% Cotton Tee"]
    assert queries.list_products(db, org, search="Co%on") == []  # % is text, not a wildcard
    assert queries.list_products(db, org, search="Co_ton") == []  # nor is _
    assert [p.title for p in queries.list_products(db, org, search="cotton")] == ["100% Cotton Tee"]
    assert len(queries.list_products(db, org, limit=10_000)) <= queries.MAX_LIMIT
