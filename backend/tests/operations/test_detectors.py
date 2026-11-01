import uuid
from datetime import timedelta
from decimal import Decimal

from app.db.models import (
    Product,
    ProductVariant,
    ReturnCase,
    ReturnCaseStatus,
    ReturnCaseType,
    Shipment,
    ShipmentStatus,
)
from app.services.operations import detectors
from tests.operations.conftest import NOW, make_customer, make_order


def test_detect_unfulfilled_orders_flags_orders_past_the_window(db, org_id):
    customer = make_customer(db, org_id)
    make_order(
        db,
        org_id,
        customer=customer,
        shopify_order_id=1,
        fulfillment_status="unfulfilled",
        created_at_shopify=NOW - timedelta(hours=72),
    )
    db.commit()
    found = detectors.detect_unfulfilled_orders(db, org_id, NOW)
    assert len(found) == 1
    assert found[0].detector_code == "ORDER_UNFULFILLED_SLA"


def test_detect_unfulfilled_orders_ignores_recent_orders(db, org_id):
    customer = make_customer(db, org_id)
    make_order(
        db,
        org_id,
        customer=customer,
        shopify_order_id=1,
        fulfillment_status="unfulfilled",
        created_at_shopify=NOW - timedelta(hours=2),
    )
    db.commit()
    assert detectors.detect_unfulfilled_orders(db, org_id, NOW) == []


def test_detect_high_value_orders(db, org_id):
    customer = make_customer(db, org_id)
    make_order(
        db,
        org_id,
        customer=customer,
        shopify_order_id=1,
        total=Decimal("9000"),
        created_at_shopify=NOW - timedelta(hours=1),
    )
    db.commit()
    found = detectors.detect_high_value_orders(db, org_id, NOW)
    assert len(found) == 1
    assert found[0].detector_code == "ORDER_HIGH_VALUE"


def test_detect_duplicate_orders_requires_same_customer_and_total(db, org_id):
    customer = make_customer(db, org_id)
    make_order(
        db,
        org_id,
        customer=customer,
        shopify_order_id=1,
        total=Decimal("50"),
        created_at_shopify=NOW,
    )
    make_order(
        db,
        org_id,
        customer=customer,
        shopify_order_id=2,
        total=Decimal("50"),
        created_at_shopify=NOW,
    )
    db.commit()
    found = detectors.detect_duplicate_orders(db, org_id, NOW)
    assert len(found) == 1
    assert found[0].detector_code == "ORDER_DUPLICATE_SIGNAL"


def test_detect_payment_issues_for_voided_order(db, org_id):
    customer = make_customer(db, org_id)
    make_order(db, org_id, customer=customer, shopify_order_id=1, financial_status="voided")
    db.commit()
    found = detectors.detect_payment_issues(db, org_id, NOW)
    assert any(d.detector_code == "PAYMENT_FAILED" for d in found)


def _make_shipment(db, org_id, order_id, **kwargs):
    shipment = Shipment(id=uuid.uuid4(), organization_id=org_id, order_id=order_id, **kwargs)
    db.add(shipment)
    db.flush()
    return shipment


def test_detect_shipment_delayed(db, org_id):
    customer = make_customer(db, org_id)
    order = make_order(db, org_id, customer=customer, shopify_order_id=1)
    _make_shipment(
        db,
        org_id,
        order.id,
        status=ShipmentStatus.IN_TRANSIT,
        estimated_delivery_at=NOW - timedelta(hours=1),
    )
    db.commit()
    found = detectors.detect_shipment_delayed(db, org_id, NOW)
    assert len(found) == 1


def test_detect_ndr_repeated_vs_single(db, org_id):
    customer = make_customer(db, org_id)
    order1 = make_order(db, org_id, customer=customer, shopify_order_id=1)
    order2 = make_order(db, org_id, customer=customer, shopify_order_id=2)
    _make_shipment(db, org_id, order1.id, status=ShipmentStatus.NDR, ndr_count=1)
    _make_shipment(db, org_id, order2.id, status=ShipmentStatus.NDR, ndr_count=3)
    db.commit()
    found = {d.detector_code for d in detectors.detect_ndr(db, org_id, NOW)}
    assert found == {"NDR_DETECTED", "NDR_REPEATED"}


def test_detect_repeated_rto_customers(db, org_id):
    customer = make_customer(db, org_id)
    order1 = make_order(db, org_id, customer=customer, shopify_order_id=1, created_at_shopify=NOW)
    order2 = make_order(db, org_id, customer=customer, shopify_order_id=2, created_at_shopify=NOW)
    _make_shipment(db, org_id, order1.id, status=ShipmentStatus.RTO)
    _make_shipment(db, org_id, order2.id, status=ShipmentStatus.RTO)
    db.commit()
    found = detectors.detect_repeated_rto_customers(db, org_id, NOW)
    assert len(found) == 1
    assert found[0].risk_level is not None


def _make_variant(db, org_id, *, inventory_quantity, available=True):
    product = Product(
        id=uuid.uuid4(),
        organization_id=org_id,
        shopify_product_id=1,
        title="T",
        status="ACTIVE",
        synced_at=NOW,
    )
    db.add(product)
    db.flush()
    variant = ProductVariant(
        id=uuid.uuid4(),
        organization_id=org_id,
        product_id=product.id,
        shopify_variant_id=1,
        title="T",
        inventory_quantity=inventory_quantity,
        available=available,
        synced_at=NOW,
    )
    db.add(variant)
    db.flush()
    return variant


def test_detect_out_of_stock(db, org_id):
    _make_variant(db, org_id, inventory_quantity=0)
    db.commit()
    found = detectors.detect_out_of_stock(db, org_id, NOW)
    assert len(found) == 1


def test_detect_low_stock(db, org_id):
    _make_variant(db, org_id, inventory_quantity=2)
    db.commit()
    found = detectors.detect_low_stock(db, org_id, NOW)
    assert len(found) == 1


def test_detect_dead_stock_requires_zero_sales_and_age(db, org_id):
    variant = _make_variant(db, org_id, inventory_quantity=20)
    variant.created_at = NOW - timedelta(days=60)
    db.commit()
    found = detectors.detect_dead_stock(db, org_id, NOW)
    assert len(found) == 1
    assert found[0].evidence["units_sold_in_window"] == 0


def test_detect_return_pending_and_refund_pending(db, org_id):
    customer = make_customer(db, org_id)
    order1 = make_order(db, org_id, customer=customer, shopify_order_id=1)
    order2 = make_order(db, org_id, customer=customer, shopify_order_id=2)
    db.add(
        ReturnCase(
            id=uuid.uuid4(),
            organization_id=org_id,
            order_id=order1.id,
            customer_id=customer.id,
            type=ReturnCaseType.RETURN,
            status=ReturnCaseStatus.REQUESTED,
            requested_at=NOW - timedelta(hours=72),
        )
    )
    db.add(
        ReturnCase(
            id=uuid.uuid4(),
            organization_id=org_id,
            order_id=order2.id,
            customer_id=customer.id,
            type=ReturnCaseType.REFUND_ONLY,
            status=ReturnCaseStatus.RECEIVED,
            requested_at=NOW - timedelta(hours=100),
        )
    )
    db.commit()
    assert len(detectors.detect_return_pending(db, org_id, NOW)) == 1
    assert len(detectors.detect_refund_pending(db, org_id, NOW)) == 1


def test_run_all_never_raises_on_an_empty_org(db, org_id):
    assert detectors.run_all(db, org_id, now=NOW) == []
