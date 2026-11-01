"""Phase 10 demo seed: realistic but clearly synthetic D2C operational data (brief section 30).

    cd backend
    python -m tools.seed_operations_demo --org-slug my-brand
    python -m tools.seed_operations_demo --org-id 11111111-1111-1111-1111-111111111111

Creates customers, products/variants, orders, shipments and return cases tagged as demo data
(synthetic Shopify ids in a reserved high range, names/emails marked "Demo"), then runs the
detection sweep so the Operations dashboard is populated immediately. Idempotent: re-running
finds existing demo rows by their reserved synthetic Shopify id instead of duplicating them.

This is a development/demo tool only -- it is never invoked from product code, and refuses to run
unless explicitly invoked (brief: "Do NOT mix demo data into production automatically"). Also
refuses to run against a production environment unless --i-know-what-im-doing is passed, the same
guard as tools/seed_platform_admin.py, since it will inject synthetic demo data into whatever
organization/database it's pointed at.
"""

import argparse
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.database import SessionLocal
from app.db.models import (
    Customer,
    Order,
    OrderItem,
    Organization,
    Product,
    ProductVariant,
    ReturnCase,
    ReturnCaseStatus,
    ReturnCaseType,
    Shipment,
    ShipmentStatus,
)
from app.services.operations import service
from app.workers.queue import InMemoryJobQueue

# Reserved synthetic Shopify id ranges -- chosen far above realistic Shopify ids to avoid ever
# colliding with real synced data, and easy to spot/filter in a support query if needed.
CUSTOMER_ID_BASE = 900_000_000
PRODUCT_ID_BASE = 900_100_000
VARIANT_ID_BASE = 900_200_000
ORDER_ID_BASE = 900_300_000
LINE_ITEM_ID_BASE = 900_400_000


def _get_or_create_customer(
    db: Session, organization_id: uuid.UUID, n: int, *, name: str
) -> Customer:
    shopify_id = CUSTOMER_ID_BASE + n
    existing = db.scalar(
        select(Customer).where(
            Customer.organization_id == organization_id, Customer.shopify_customer_id == shopify_id
        )
    )
    if existing is not None:
        return existing
    customer = Customer(
        id=uuid.uuid4(),
        organization_id=organization_id,
        shopify_customer_id=shopify_id,
        email=f"demo.customer.{n}@example.test",
        first_name=f"Demo {name}",
        normalized_email=f"demo.customer.{n}@example.test",
    )
    db.add(customer)
    db.flush()
    return customer


def _get_or_create_product(
    db: Session, organization_id: uuid.UUID, n: int, *, title: str, inventory_quantity: int
) -> ProductVariant:
    product_shopify_id = PRODUCT_ID_BASE + n
    variant_shopify_id = VARIANT_ID_BASE + n
    now = datetime.now(UTC)
    product = db.scalar(
        select(Product).where(
            Product.organization_id == organization_id,
            Product.shopify_product_id == product_shopify_id,
        )
    )
    if product is None:
        product = Product(
            id=uuid.uuid4(),
            organization_id=organization_id,
            shopify_product_id=product_shopify_id,
            title=f"[Demo] {title}",
            status="ACTIVE",
            synced_at=now,
        )
        db.add(product)
        db.flush()
    variant = db.scalar(
        select(ProductVariant).where(
            ProductVariant.organization_id == organization_id,
            ProductVariant.shopify_variant_id == variant_shopify_id,
        )
    )
    if variant is None:
        variant = ProductVariant(
            id=uuid.uuid4(),
            organization_id=organization_id,
            product_id=product.id,
            shopify_variant_id=variant_shopify_id,
            title=f"[Demo] {title}",
            sku=f"DEMO-SKU-{n}",
            price=Decimal("1999.00"),
            inventory_quantity=inventory_quantity,
            available=True,
            synced_at=now,
        )
        db.add(variant)
        db.flush()
    else:
        variant.inventory_quantity = inventory_quantity
    return variant


def _get_or_create_order(
    db: Session,
    organization_id: uuid.UUID,
    n: int,
    *,
    customer: Customer,
    variant: ProductVariant | None,
    total: Decimal,
    financial_status: str,
    fulfillment_status: str | None,
    created_days_ago: float,
) -> Order:
    shopify_id = ORDER_ID_BASE + n
    now = datetime.now(UTC)
    created_at = now - timedelta(days=created_days_ago)
    order = db.scalar(
        select(Order).where(
            Order.organization_id == organization_id, Order.shopify_order_id == shopify_id
        )
    )
    if order is not None:
        return order
    order = Order(
        id=uuid.uuid4(),
        organization_id=organization_id,
        shopify_order_id=shopify_id,
        order_number=f"#DEMO{n}",
        customer_id=customer.id,
        financial_status=financial_status,
        fulfillment_status=fulfillment_status,
        currency="USD",
        subtotal=total,
        total=total,
        created_at_shopify=created_at,
        updated_at_shopify=created_at,
        synced_at=now,
    )
    db.add(order)
    db.flush()
    if variant is not None:
        db.add(
            OrderItem(
                id=uuid.uuid4(),
                organization_id=organization_id,
                order_id=order.id,
                product_id=variant.product_id,
                variant_id=variant.id,
                shopify_line_item_id=LINE_ITEM_ID_BASE + n,
                title=variant.title,
                sku=variant.sku,
                quantity=1,
                price=total,
            )
        )
    return order


def _get_or_create_shipment(
    db: Session,
    organization_id: uuid.UUID,
    order: Order,
    *,
    status: ShipmentStatus,
    tracking_number: str | None,
    shipped_days_ago: float | None,
    estimated_days_from_ship: float | None,
    ndr_count: int = 0,
) -> Shipment:
    existing = db.scalar(
        select(Shipment).where(
            Shipment.organization_id == organization_id, Shipment.order_id == order.id
        )
    )
    if existing is not None:
        return existing
    now = datetime.now(UTC)
    shipped_at = now - timedelta(days=shipped_days_ago) if shipped_days_ago is not None else None
    estimated_at = (
        shipped_at + timedelta(days=estimated_days_from_ship)
        if shipped_at and estimated_days_from_ship is not None
        else None
    )
    shipment = Shipment(
        id=uuid.uuid4(),
        organization_id=organization_id,
        order_id=order.id,
        carrier="Demo Carrier",
        tracking_number=tracking_number,
        status=status,
        shipped_at=shipped_at,
        estimated_delivery_at=estimated_at,
        ndr_count=ndr_count,
        ndr_reason="Customer unavailable" if ndr_count else None,
    )
    db.add(shipment)
    db.flush()
    return shipment


def _get_or_create_return_case(
    db: Session,
    organization_id: uuid.UUID,
    order: Order,
    customer: Customer,
    *,
    type_: ReturnCaseType,
    status: ReturnCaseStatus,
    requested_days_ago: float,
) -> ReturnCase:
    existing = db.scalar(
        select(ReturnCase).where(
            ReturnCase.organization_id == organization_id, ReturnCase.order_id == order.id
        )
    )
    if existing is not None:
        return existing
    case = ReturnCase(
        id=uuid.uuid4(),
        organization_id=organization_id,
        order_id=order.id,
        customer_id=customer.id,
        type=type_,
        reason="Demo return reason",
        status=status,
        requested_at=datetime.now(UTC) - timedelta(days=requested_days_ago),
    )
    db.add(case)
    db.flush()
    return case


def seed(db: Session, organization_id: uuid.UUID) -> dict[str, int]:
    customers = [
        _get_or_create_customer(db, organization_id, i, name=f"Shopper {i}") for i in range(1, 7)
    ]
    variants = [
        _get_or_create_product(db, organization_id, 1, title="Everyday Tee", inventory_quantity=0),
        _get_or_create_product(
            db, organization_id, 2, title="Low Stock Hoodie", inventory_quantity=3
        ),
        _get_or_create_product(
            db, organization_id, 3, title="Dead Stock Cap", inventory_quantity=50
        ),
        _get_or_create_product(
            db, organization_id, 4, title="Bestseller Socks", inventory_quantity=200
        ),
    ]
    db.flush()

    orders = [
        _get_or_create_order(
            db,
            organization_id,
            1,
            customer=customers[0],
            variant=variants[0],
            total=Decimal("49.00"),
            financial_status="paid",
            fulfillment_status="unfulfilled",
            created_days_ago=4,
        ),
        _get_or_create_order(
            db,
            organization_id,
            2,
            customer=customers[1],
            variant=variants[1],
            total=Decimal("7200.00"),
            financial_status="paid",
            fulfillment_status="unfulfilled",
            created_days_ago=1,
        ),
        _get_or_create_order(
            db,
            organization_id,
            3,
            customer=customers[2],
            variant=variants[3],
            total=Decimal("89.00"),
            financial_status="pending",
            fulfillment_status=None,
            created_days_ago=2,
        ),
        _get_or_create_order(
            db,
            organization_id,
            4,
            customer=customers[3],
            variant=variants[3],
            total=Decimal("120.00"),
            financial_status="paid",
            fulfillment_status="fulfilled",
            created_days_ago=10,
        ),
        _get_or_create_order(
            db,
            organization_id,
            5,
            customer=customers[4],
            variant=variants[0],
            total=Decimal("60.00"),
            financial_status="paid",
            fulfillment_status="fulfilled",
            created_days_ago=8,
        ),
        _get_or_create_order(
            db,
            organization_id,
            6,
            customer=customers[5],
            variant=variants[0],
            total=Decimal("60.00"),
            financial_status="paid",
            fulfillment_status="fulfilled",
            created_days_ago=6,
        ),
        _get_or_create_order(
            db,
            organization_id,
            7,
            customer=customers[5],
            variant=variants[0],
            total=Decimal("60.00"),
            financial_status="paid",
            fulfillment_status="fulfilled",
            created_days_ago=3,
        ),
    ]
    db.flush()

    # order[3]: shipment delayed past estimate.
    _get_or_create_shipment(
        db,
        organization_id,
        orders[3],
        status=ShipmentStatus.IN_TRANSIT,
        tracking_number="DEMO-TRACK-1",
        shipped_days_ago=6,
        estimated_days_from_ship=2,
    )
    # order[4]: repeated NDR.
    _get_or_create_shipment(
        db,
        organization_id,
        orders[4],
        status=ShipmentStatus.NDR,
        tracking_number="DEMO-TRACK-2",
        shipped_days_ago=3,
        estimated_days_from_ship=2,
        ndr_count=2,
    )
    # orders[5] and [6]: same customer, both RTO -> repeated-RTO risk signal.
    _get_or_create_shipment(
        db,
        organization_id,
        orders[5],
        status=ShipmentStatus.RTO,
        tracking_number="DEMO-TRACK-3",
        shipped_days_ago=5,
        estimated_days_from_ship=2,
    )
    _get_or_create_shipment(
        db,
        organization_id,
        orders[6],
        status=ShipmentStatus.RTO,
        tracking_number="DEMO-TRACK-4",
        shipped_days_ago=2,
        estimated_days_from_ship=2,
    )

    # Return/refund/exchange cases.
    _get_or_create_return_case(
        db,
        organization_id,
        orders[3],
        customers[3],
        type_=ReturnCaseType.RETURN,
        status=ReturnCaseStatus.REQUESTED,
        requested_days_ago=3,
    )
    extra_order = _get_or_create_order(
        db,
        organization_id,
        8,
        customer=customers[2],
        variant=variants[2],
        total=Decimal("35.00"),
        financial_status="refunded",
        fulfillment_status="fulfilled",
        created_days_ago=12,
    )
    _get_or_create_return_case(
        db,
        organization_id,
        extra_order,
        customers[2],
        type_=ReturnCaseType.REFUND_ONLY,
        status=ReturnCaseStatus.RECEIVED,
        requested_days_ago=4,
    )
    exchange_order = _get_or_create_order(
        db,
        organization_id,
        9,
        customer=customers[4],
        variant=variants[1],
        total=Decimal("65.00"),
        financial_status="paid",
        fulfillment_status="fulfilled",
        created_days_ago=9,
    )
    _get_or_create_return_case(
        db,
        organization_id,
        exchange_order,
        customers[4],
        type_=ReturnCaseType.EXCHANGE,
        status=ReturnCaseStatus.REQUESTED,
        requested_days_ago=4,
    )

    db.commit()

    queue = InMemoryJobQueue()
    result = service.run_detection_sweep(db, queue, organization_id=organization_id)
    return {
        "customers": len(customers),
        "orders": len(orders) + 2,
        "issues_created": result.created,
        "issues_updated": result.updated,
        "sla_breaches": result.breached,
    }


def main(argv: Sequence[str] | None = None, session_factory=SessionLocal) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--org-id", type=uuid.UUID, help="organization UUID to seed")
    group.add_argument("--org-slug", type=str, help="organization slug to seed")
    parser.add_argument(
        "--i-know-what-im-doing",
        action="store_true",
        help="required to run this against a production environment",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.is_production and not args.i_know_what_im_doing:
        print(
            "FAIL  refusing to run against a production environment without --i-know-what-im-doing"
        )
        return 2

    with session_factory() as db:
        if args.org_id is not None:
            org = db.get(Organization, args.org_id)
        else:
            org = db.scalar(select(Organization).where(Organization.slug == args.org_slug))
        if org is None:
            print("FAIL  organization not found")
            return 2
        counts = seed(db, org.id)

    print(
        f"OK    seeded demo operations data for organization {org.id} ({org.slug}): "
        f"{counts['customers']} customers, {counts['orders']} orders, "
        f"{counts['issues_created']} issues created, {counts['issues_updated']} updated, "
        f"{counts['sla_breaches']} already-breached. Open /operations to view."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
