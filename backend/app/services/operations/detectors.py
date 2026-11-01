"""Deterministic operational detectors (brief section 4). No LLM call anywhere in this module --
every function here is a plain SQL query plus arithmetic, returns a bounded list of
``issues.Detection`` objects, and NEVER fabricates a fact the source data cannot support (a
detector that needs a field the current sync does not populate is simply not implemented, and is
listed in ``DETECTOR_REQUIREMENTS``/``docs/operations-architecture.md`` as a known limitation
rather than guessed at).

Full coverage (real detectors, demo-depth): ORDER, SHIPPING/NDR/RTO, INVENTORY.
Lighter coverage reusing existing Shopify fields with no new tables: PAYMENT (from
``Order.financial_status``), RETURN/REFUND/EXCHANGE (from ``ReturnCase``), FRAUD/risk (from
repeated RTO + order duplication signals already computed by the detectors above).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    DetectedBy,
    IssueCategory,
    IssueSeverity,
    Order,
    OrderItem,
    ProductVariant,
    ReturnCase,
    ReturnCaseStatus,
    ReturnCaseType,
    RiskLevel,
    Shipment,
    ShipmentStatus,
)
from app.services.operations.issues import Detection

UNFULFILLED_HOURS = 48
HIGH_VALUE_THRESHOLD = Decimal("5000")
DUPLICATE_WINDOW_HOURS = 24
LOW_STOCK_THRESHOLD = 5
VELOCITY_WINDOW_DAYS = 30
RETURN_PENDING_HOURS = 48
REFUND_PENDING_HOURS = 72
EXCHANGE_PENDING_HOURS = 72
PAYMENT_PENDING_HOURS = 24
MAX_ROWS = 500

# Brief section 4: "A detector must explicitly declare its required data."
DETECTOR_REQUIREMENTS: dict[str, str] = {
    "ORDER_UNFULFILLED_SLA": "orders.fulfillment_status, orders.created_at_shopify",
    "ORDER_HIGH_VALUE": "orders.total, orders.cancelled_at",
    "ORDER_DUPLICATE_SIGNAL": "orders.customer_id, orders.total, orders.created_at_shopify",
    "ORDER_PARTIAL_FULFILLMENT_STUCK": "orders.fulfillment_status, orders.updated_at_shopify",
    "PAYMENT_PENDING": "orders.financial_status, orders.created_at_shopify",
    "PAYMENT_FAILED": "orders.financial_status",
    "SHIPMENT_DELAYED": "shipments.status, shipments.estimated_delivery_at",
    "TRACKING_MISSING": "shipments.tracking_number, shipments.shipped_at",
    "NDR_DETECTED": "shipments.status",
    "NDR_REPEATED": "shipments.ndr_count",
    "RTO_DETECTED": "shipments.status",
    "RTO_REPEATED_CUSTOMER": "shipments.status, orders.customer_id",
    "INVENTORY_OUT_OF_STOCK": "product_variants.inventory_quantity, product_variants.available",
    "INVENTORY_LOW_STOCK": "product_variants.inventory_quantity",
    "INVENTORY_DEAD_STOCK": "product_variants.inventory_quantity, order_items (30d velocity)",
    "RETURN_PENDING": "return_cases.status, return_cases.requested_at",
    "RETURN_PICKUP_FAILED": "return_cases.status",
    "REFUND_PENDING": "return_cases.status, return_cases.requested_at",
    "EXCHANGE_PENDING": "return_cases.type, return_cases.status",
    "FRAUD_RISK_SIGNAL": "shipments.status (RTO), orders.customer_id, orders.total",
}

# Known limitations (not implemented because the field does not exist in the current sync):
# COD detection (no payment gateway/method column), address/pincode issues (no shipping address
# columns), duplicate-payment signal (no payment gateway transaction id column). See
# docs/operations-architecture.md.


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _hours_between(a: datetime, b: datetime) -> float:
    return (_ensure_utc(a) - _ensure_utc(b)).total_seconds() / 3600


# --- ORDER --------------------------------------------------------------------------------------


def detect_unfulfilled_orders(
    db: Session, organization_id: uuid.UUID, now: datetime
) -> list[Detection]:
    cutoff = now - timedelta(hours=UNFULFILLED_HOURS)
    rows = db.scalars(
        select(Order)
        .where(
            Order.organization_id == organization_id,
            Order.cancelled_at.is_(None),
            Order.fulfillment_status.in_([None, "unfulfilled", "partial"]),
            Order.created_at_shopify.is_not(None),
            Order.created_at_shopify <= cutoff,
        )
        .limit(MAX_ROWS)
    ).all()
    out = []
    for order in rows:
        if order.created_at_shopify is None:
            continue
        days_open = round(_hours_between(now, order.created_at_shopify) / 24, 1)
        out.append(
            Detection(
                organization_id=organization_id,
                category=IssueCategory.ORDER,
                detector_code="ORDER_UNFULFILLED_SLA",
                dedupe_key=f"ORDER_UNFULFILLED_SLA:{order.id}",
                title=f"Order {order.order_number or order.id} unfulfilled for {days_open} days",
                description="Order has not been fulfilled within the expected window.",
                evidence={
                    "order_id": str(order.id),
                    "order_number": order.order_number,
                    "days_open": days_open,
                    "expected_sla_hours": UNFULFILLED_HOURS,
                    "fulfillment_status": order.fulfillment_status,
                },
                order_id=order.id,
                customer_id=order.customer_id,
                recommended_action=(
                    "Fulfill the order or contact the customer with an updated timeline."
                ),
                severity=IssueSeverity.HIGH if days_open >= 4 else IssueSeverity.MEDIUM,
                order_value=order.total,
            )
        )
    return out


def detect_high_value_orders(
    db: Session,
    organization_id: uuid.UUID,
    now: datetime,
    *,
    threshold: Decimal = HIGH_VALUE_THRESHOLD,
) -> list[Detection]:
    cutoff = now - timedelta(days=7)
    rows = db.scalars(
        select(Order)
        .where(
            Order.organization_id == organization_id,
            Order.cancelled_at.is_(None),
            Order.total.is_not(None),
            Order.total >= threshold,
            Order.created_at_shopify.is_not(None),
            Order.created_at_shopify >= cutoff,
        )
        .limit(MAX_ROWS)
    ).all()
    return [
        Detection(
            organization_id=organization_id,
            category=IssueCategory.ORDER,
            detector_code="ORDER_HIGH_VALUE",
            dedupe_key=f"ORDER_HIGH_VALUE:{order.id}",
            title=f"High-value order {order.order_number or order.id} needs attention",
            description="Order value exceeds the high-value threshold; prioritize review.",
            evidence={
                "order_id": str(order.id),
                "total": str(order.total),
                "threshold": str(threshold),
            },
            order_id=order.id,
            customer_id=order.customer_id,
            recommended_action="Review for fraud risk and prioritize fulfillment.",
            severity=IssueSeverity.MEDIUM,
            order_value=order.total,
        )
        for order in rows
    ]


def detect_duplicate_orders(
    db: Session, organization_id: uuid.UUID, now: datetime
) -> list[Detection]:
    cutoff = now - timedelta(hours=DUPLICATE_WINDOW_HOURS)
    rows = db.scalars(
        select(Order)
        .where(
            Order.organization_id == organization_id,
            Order.customer_id.is_not(None),
            Order.cancelled_at.is_(None),
            Order.created_at_shopify.is_not(None),
            Order.created_at_shopify >= cutoff,
        )
        .limit(MAX_ROWS)
    ).all()
    groups: dict[tuple[uuid.UUID, str], list[Order]] = {}
    for order in rows:
        if order.customer_id is None or order.created_at_shopify is None:
            continue
        key = (order.customer_id, str(order.total))
        groups.setdefault(key, []).append(order)
    out = []
    for (customer_id, total), group in groups.items():
        if len(group) < 2:
            continue
        newest = max(group, key=lambda o: o.created_at_shopify or now)
        out.append(
            Detection(
                organization_id=organization_id,
                category=IssueCategory.ORDER,
                detector_code="ORDER_DUPLICATE_SIGNAL",
                dedupe_key=f"ORDER_DUPLICATE_SIGNAL:{customer_id}:{total}:{DUPLICATE_WINDOW_HOURS}",
                title="Possible duplicate order detected",
                description=f"{len(group)} orders with the same total from one customer within "
                f"{DUPLICATE_WINDOW_HOURS}h.",
                evidence={
                    "customer_id": str(customer_id),
                    "order_ids": [str(o.id) for o in group],
                    "total": total,
                    "window_hours": DUPLICATE_WINDOW_HOURS,
                },
                order_id=newest.id,
                customer_id=customer_id,
                recommended_action="Confirm with the customer before fulfilling more than one.",
                severity=IssueSeverity.MEDIUM,
                order_value=newest.total,
            )
        )
    return out


def detect_partial_fulfillment_stuck(
    db: Session, organization_id: uuid.UUID, now: datetime
) -> list[Detection]:
    cutoff = now - timedelta(hours=UNFULFILLED_HOURS)
    rows = db.scalars(
        select(Order)
        .where(
            Order.organization_id == organization_id,
            Order.cancelled_at.is_(None),
            Order.fulfillment_status == "partial",
            Order.updated_at_shopify.is_not(None),
            Order.updated_at_shopify <= cutoff,
        )
        .limit(MAX_ROWS)
    ).all()
    return [
        Detection(
            organization_id=organization_id,
            category=IssueCategory.WAREHOUSE,
            detector_code="ORDER_PARTIAL_FULFILLMENT_STUCK",
            dedupe_key=f"ORDER_PARTIAL_FULFILLMENT_STUCK:{order.id}",
            title=f"Order {order.order_number or order.id} stuck partially fulfilled",
            description="Order has been partially fulfilled with no update for over "
            f"{UNFULFILLED_HOURS}h.",
            evidence={"order_id": str(order.id), "fulfillment_status": order.fulfillment_status},
            order_id=order.id,
            customer_id=order.customer_id,
            recommended_action="Check warehouse pick/pack queue for the remaining line items.",
            severity=IssueSeverity.MEDIUM,
            order_value=order.total,
        )
        for order in rows
    ]


# --- PAYMENT (reused from Order.financial_status; no new table) --------------------------------


def detect_payment_issues(
    db: Session, organization_id: uuid.UUID, now: datetime
) -> list[Detection]:
    cutoff = now - timedelta(hours=PAYMENT_PENDING_HOURS)
    rows = db.scalars(
        select(Order)
        .where(
            Order.organization_id == organization_id,
            Order.cancelled_at.is_(None),
            Order.financial_status.in_(["pending", "voided", "expired"]),
        )
        .limit(MAX_ROWS)
    ).all()
    out = []
    for order in rows:
        if order.financial_status == "pending":
            if order.created_at_shopify is None or order.created_at_shopify > cutoff:
                continue
            code, title, severity = (
                "PAYMENT_PENDING",
                f"Payment pending for order {order.order_number or order.id}",
                IssueSeverity.MEDIUM,
            )
        else:
            code, title, severity = (
                "PAYMENT_FAILED",
                f"Payment failed/expired for order {order.order_number or order.id}",
                IssueSeverity.HIGH,
            )
        out.append(
            Detection(
                organization_id=organization_id,
                category=IssueCategory.PAYMENT,
                detector_code=code,
                dedupe_key=f"{code}:{order.id}",
                title=title,
                description=f"Order financial_status is '{order.financial_status}'.",
                evidence={"order_id": str(order.id), "financial_status": order.financial_status},
                order_id=order.id,
                customer_id=order.customer_id,
                recommended_action="Follow up on payment before continuing fulfillment.",
                severity=severity,
                order_value=order.total,
            )
        )
    return out


# --- SHIPPING / NDR / RTO (Shipment) -------------------------------------------------------------


def detect_shipment_delayed(
    db: Session, organization_id: uuid.UUID, now: datetime
) -> list[Detection]:
    rows = db.scalars(
        select(Shipment)
        .where(
            Shipment.organization_id == organization_id,
            Shipment.status.in_(
                [ShipmentStatus.PENDING, ShipmentStatus.IN_TRANSIT, ShipmentStatus.OUT_FOR_DELIVERY]
            ),
            Shipment.estimated_delivery_at.is_not(None),
            Shipment.estimated_delivery_at <= now,
        )
        .limit(MAX_ROWS)
    ).all()
    return [
        Detection(
            organization_id=organization_id,
            category=IssueCategory.SHIPPING,
            detector_code="SHIPMENT_DELAYED",
            dedupe_key=f"SHIPMENT_DELAYED:{shipment.id}",
            title="Shipment delayed past estimated delivery",
            description=(
                "Shipment is past its estimated delivery date without being marked delivered."
            ),
            evidence={
                "shipment_id": str(shipment.id),
                "order_id": str(shipment.order_id),
                "status": shipment.status.value,
                "estimated_delivery_at": (
                    shipment.estimated_delivery_at.isoformat()
                    if shipment.estimated_delivery_at
                    else None
                ),
            },
            order_id=shipment.order_id,
            recommended_action="Check with the carrier and notify the customer proactively.",
            severity=IssueSeverity.MEDIUM,
            detected_by=DetectedBy.RULE,
        )
        for shipment in rows
    ]


def detect_tracking_missing(
    db: Session, organization_id: uuid.UUID, now: datetime
) -> list[Detection]:
    rows = db.scalars(
        select(Shipment)
        .where(
            Shipment.organization_id == organization_id,
            Shipment.shipped_at.is_not(None),
            Shipment.tracking_number.is_(None),
            Shipment.status != ShipmentStatus.DELIVERED,
        )
        .limit(MAX_ROWS)
    ).all()
    return [
        Detection(
            organization_id=organization_id,
            category=IssueCategory.SHIPPING,
            detector_code="TRACKING_MISSING",
            dedupe_key=f"TRACKING_MISSING:{shipment.id}",
            title="Shipment marked shipped with no tracking number",
            evidence={"shipment_id": str(shipment.id), "order_id": str(shipment.order_id)},
            order_id=shipment.order_id,
            recommended_action="Obtain tracking from the carrier and update the shipment record.",
            severity=IssueSeverity.LOW,
        )
        for shipment in rows
    ]


def detect_ndr(db: Session, organization_id: uuid.UUID, now: datetime) -> list[Detection]:
    rows = db.scalars(
        select(Shipment)
        .where(Shipment.organization_id == organization_id, Shipment.status == ShipmentStatus.NDR)
        .limit(MAX_ROWS)
    ).all()
    out = []
    for shipment in rows:
        repeated = shipment.ndr_count >= 2
        code = "NDR_REPEATED" if repeated else "NDR_DETECTED"
        out.append(
            Detection(
                organization_id=organization_id,
                category=IssueCategory.NDR,
                detector_code=code,
                dedupe_key=f"{code}:{shipment.id}",
                title="Repeated non-delivery (NDR)"
                if repeated
                else "Non-delivery report (NDR) detected",
                evidence={
                    "shipment_id": str(shipment.id),
                    "order_id": str(shipment.order_id),
                    "ndr_count": shipment.ndr_count,
                    "ndr_reason": shipment.ndr_reason,
                },
                order_id=shipment.order_id,
                recommended_action=(
                    "Contact the customer through WhatsApp and confirm delivery availability."
                ),
                severity=IssueSeverity.HIGH if repeated else IssueSeverity.MEDIUM,
                repeated_incidents=shipment.ndr_count,
            )
        )
    return out


def detect_rto(db: Session, organization_id: uuid.UUID, now: datetime) -> list[Detection]:
    rows = db.scalars(
        select(Shipment)
        .where(Shipment.organization_id == organization_id, Shipment.status == ShipmentStatus.RTO)
        .limit(MAX_ROWS)
    ).all()
    return [
        Detection(
            organization_id=organization_id,
            category=IssueCategory.RTO,
            detector_code="RTO_DETECTED",
            dedupe_key=f"RTO_DETECTED:{shipment.id}",
            title="Return-to-origin (RTO) detected",
            evidence={"shipment_id": str(shipment.id), "order_id": str(shipment.order_id)},
            order_id=shipment.order_id,
            recommended_action="Inspect the returned item and decide on restock vs. write-off.",
            severity=IssueSeverity.MEDIUM,
        )
        for shipment in rows
    ]


def detect_repeated_rto_customers(
    db: Session, organization_id: uuid.UUID, now: datetime
) -> list[Detection]:
    rows = db.execute(
        select(Order.customer_id, func.count(Shipment.id).label("rto_count"))
        .join(Shipment, Shipment.order_id == Order.id)
        .where(
            Order.organization_id == organization_id,
            Shipment.organization_id == organization_id,
            Shipment.status == ShipmentStatus.RTO,
            Order.customer_id.is_not(None),
        )
        .group_by(Order.customer_id)
        .having(func.count(Shipment.id) >= 2)
    ).all()
    out = []
    for customer_id, rto_count in rows:
        latest_order_id = db.scalar(
            select(Order.id)
            .join(Shipment, Shipment.order_id == Order.id)
            .where(
                Order.organization_id == organization_id,
                Order.customer_id == customer_id,
                Shipment.status == ShipmentStatus.RTO,
            )
            .order_by(Order.created_at_shopify.desc())
            .limit(1)
        )
        out.append(
            Detection(
                organization_id=organization_id,
                category=IssueCategory.RTO,
                detector_code="RTO_REPEATED_CUSTOMER",
                dedupe_key=f"RTO_REPEATED_CUSTOMER:{customer_id}",
                title="Customer with repeated RTO",
                description=f"{rto_count} return-to-origin shipments for this customer.",
                evidence={"customer_id": str(customer_id), "rto_count": rto_count},
                order_id=latest_order_id,
                customer_id=customer_id,
                recommended_action="Review before accepting further COD orders from this customer.",
                severity=IssueSeverity.HIGH if rto_count >= 3 else IssueSeverity.MEDIUM,
                repeated_incidents=rto_count,
                risk_level=RiskLevel.HIGH if rto_count >= 3 else RiskLevel.MEDIUM,
            )
        )
    return out


# --- INVENTORY -----------------------------------------------------------------------------------


def detect_out_of_stock(db: Session, organization_id: uuid.UUID, now: datetime) -> list[Detection]:
    rows = db.scalars(
        select(ProductVariant)
        .where(
            ProductVariant.organization_id == organization_id,
            ProductVariant.available.is_(True),
            ProductVariant.inventory_quantity == 0,
            ProductVariant.deleted_at.is_(None),
        )
        .limit(MAX_ROWS)
    ).all()
    return [
        Detection(
            organization_id=organization_id,
            category=IssueCategory.INVENTORY,
            detector_code="INVENTORY_OUT_OF_STOCK",
            dedupe_key=f"INVENTORY_OUT_OF_STOCK:{variant.id}",
            title=f"Out of stock: {variant.title}",
            evidence={"variant_id": str(variant.id), "sku": variant.sku, "inventory_quantity": 0},
            recommended_action="Restock or mark the product unavailable to avoid overselling.",
            severity=IssueSeverity.HIGH,
        )
        for variant in rows
    ]


def detect_low_stock(
    db: Session, organization_id: uuid.UUID, now: datetime, *, threshold: int = LOW_STOCK_THRESHOLD
) -> list[Detection]:
    rows = db.scalars(
        select(ProductVariant)
        .where(
            ProductVariant.organization_id == organization_id,
            ProductVariant.inventory_quantity.is_not(None),
            ProductVariant.inventory_quantity > 0,
            ProductVariant.inventory_quantity <= threshold,
            ProductVariant.deleted_at.is_(None),
        )
        .limit(MAX_ROWS)
    ).all()
    return [
        Detection(
            organization_id=organization_id,
            category=IssueCategory.INVENTORY,
            detector_code="INVENTORY_LOW_STOCK",
            dedupe_key=f"INVENTORY_LOW_STOCK:{variant.id}",
            title=f"Low stock: {variant.title} ({variant.inventory_quantity} left)",
            evidence={
                "variant_id": str(variant.id),
                "sku": variant.sku,
                "inventory_quantity": variant.inventory_quantity,
                "threshold": threshold,
            },
            recommended_action="Reorder soon to avoid a stockout.",
            severity=IssueSeverity.MEDIUM,
        )
        for variant in rows
    ]


def _velocity_by_variant(
    db: Session,
    organization_id: uuid.UUID,
    now: datetime,
    *,
    window_days: int = VELOCITY_WINDOW_DAYS,
) -> dict[uuid.UUID, float]:
    cutoff = now - timedelta(days=window_days)
    rows = db.execute(
        select(OrderItem.variant_id, func.sum(OrderItem.quantity))
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            OrderItem.organization_id == organization_id,
            OrderItem.variant_id.is_not(None),
            Order.cancelled_at.is_(None),
            Order.created_at_shopify.is_not(None),
            Order.created_at_shopify >= cutoff,
        )
        .group_by(OrderItem.variant_id)
    ).all()
    return {variant_id: (total_qty or 0) / window_days for variant_id, total_qty in rows}


def detect_dead_stock(db: Session, organization_id: uuid.UUID, now: datetime) -> list[Detection]:
    """Units-sold-per-day comes from real ``OrderItem`` rows (never fabricated); a variant with
    zero sales in the trailing window AND stock on hand AND an old enough listing is flagged."""
    velocity = _velocity_by_variant(db, organization_id, now)
    stale_cutoff = now - timedelta(days=VELOCITY_WINDOW_DAYS)
    rows = db.scalars(
        select(ProductVariant)
        .where(
            ProductVariant.organization_id == organization_id,
            ProductVariant.inventory_quantity.is_not(None),
            ProductVariant.inventory_quantity > 0,
            ProductVariant.deleted_at.is_(None),
            ProductVariant.created_at <= stale_cutoff,
        )
        .limit(MAX_ROWS)
    ).all()
    out = []
    for variant in rows:
        if velocity.get(variant.id, 0.0) > 0:
            continue
        out.append(
            Detection(
                organization_id=organization_id,
                category=IssueCategory.INVENTORY,
                detector_code="INVENTORY_DEAD_STOCK",
                dedupe_key=f"INVENTORY_DEAD_STOCK:{variant.id}",
                title=f"Dead stock: {variant.title}",
                description=f"No sales in the last {VELOCITY_WINDOW_DAYS} days with stock on hand.",
                evidence={
                    "variant_id": str(variant.id),
                    "sku": variant.sku,
                    "inventory_quantity": variant.inventory_quantity,
                    "window_days": VELOCITY_WINDOW_DAYS,
                    "units_sold_in_window": 0,
                },
                recommended_action="Consider a promotion or discontinuing the SKU.",
                severity=IssueSeverity.LOW,
            )
        )
    return out


@dataclass(frozen=True)
class StockoutProjection:
    variant_id: uuid.UUID
    days_of_stock: float | None
    velocity_per_day: float


def project_stockout(
    db: Session, organization_id: uuid.UUID, variant_id: uuid.UUID, now: datetime
) -> StockoutProjection | None:
    """Used by Operations AI to answer "when will X stock out" -- only ever called with data that
    exists; returns None (never a guess) when the variant or its inventory count is unknown."""
    variant = db.get(ProductVariant, variant_id)
    if (
        variant is None
        or variant.organization_id != organization_id
        or variant.inventory_quantity is None
    ):
        return None
    velocity = _velocity_by_variant(db, organization_id, now).get(variant_id, 0.0)
    days_of_stock = (variant.inventory_quantity / velocity) if velocity > 0 else None
    return StockoutProjection(
        variant_id=variant_id, days_of_stock=days_of_stock, velocity_per_day=velocity
    )


# --- RETURN / REFUND / EXCHANGE (ReturnCase) ------------------------------------------------------


def detect_return_pending(
    db: Session, organization_id: uuid.UUID, now: datetime
) -> list[Detection]:
    cutoff = now - timedelta(hours=RETURN_PENDING_HOURS)
    rows = db.scalars(
        select(ReturnCase)
        .where(
            ReturnCase.organization_id == organization_id,
            ReturnCase.status == ReturnCaseStatus.REQUESTED,
            ReturnCase.requested_at <= cutoff,
        )
        .limit(MAX_ROWS)
    ).all()
    return [
        Detection(
            organization_id=organization_id,
            category=IssueCategory.RETURN,
            detector_code="RETURN_PENDING",
            dedupe_key=f"RETURN_PENDING:{case.id}",
            title="Return request awaiting action",
            evidence={
                "return_case_id": str(case.id),
                "order_id": str(case.order_id),
                "status": case.status.value,
            },
            order_id=case.order_id,
            customer_id=case.customer_id,
            recommended_action="Approve the return and schedule pickup.",
            severity=IssueSeverity.MEDIUM,
        )
        for case in rows
    ]


def detect_pickup_failed(db: Session, organization_id: uuid.UUID, now: datetime) -> list[Detection]:
    rows = db.scalars(
        select(ReturnCase)
        .where(
            ReturnCase.organization_id == organization_id,
            ReturnCase.status == ReturnCaseStatus.PICKUP_FAILED,
        )
        .limit(MAX_ROWS)
    ).all()
    return [
        Detection(
            organization_id=organization_id,
            category=IssueCategory.RETURN,
            detector_code="RETURN_PICKUP_FAILED",
            dedupe_key=f"RETURN_PICKUP_FAILED:{case.id}",
            title="Return pickup failed",
            evidence={"return_case_id": str(case.id), "order_id": str(case.order_id)},
            order_id=case.order_id,
            customer_id=case.customer_id,
            recommended_action="Reschedule the pickup or arrange a drop-off alternative.",
            severity=IssueSeverity.MEDIUM,
        )
        for case in rows
    ]


def detect_refund_pending(
    db: Session, organization_id: uuid.UUID, now: datetime
) -> list[Detection]:
    cutoff = now - timedelta(hours=REFUND_PENDING_HOURS)
    rows = db.scalars(
        select(ReturnCase)
        .where(
            ReturnCase.organization_id == organization_id,
            ReturnCase.status == ReturnCaseStatus.RECEIVED,
            ReturnCase.requested_at <= cutoff,
        )
        .limit(MAX_ROWS)
    ).all()
    return [
        Detection(
            organization_id=organization_id,
            category=IssueCategory.REFUND,
            detector_code="REFUND_PENDING",
            dedupe_key=f"REFUND_PENDING:{case.id}",
            title="Refund pending on a received return",
            evidence={"return_case_id": str(case.id), "order_id": str(case.order_id)},
            order_id=case.order_id,
            customer_id=case.customer_id,
            recommended_action="Issue the refund; the returned item has been received.",
            severity=IssueSeverity.HIGH,
        )
        for case in rows
    ]


def detect_exchange_pending(
    db: Session, organization_id: uuid.UUID, now: datetime
) -> list[Detection]:
    cutoff = now - timedelta(hours=EXCHANGE_PENDING_HOURS)
    rows = db.scalars(
        select(ReturnCase)
        .where(
            ReturnCase.organization_id == organization_id,
            ReturnCase.type == ReturnCaseType.EXCHANGE,
            ReturnCase.status.not_in(
                [ReturnCaseStatus.EXCHANGED, ReturnCaseStatus.REJECTED, ReturnCaseStatus.CANCELLED]
            ),
            ReturnCase.requested_at <= cutoff,
        )
        .limit(MAX_ROWS)
    ).all()
    return [
        Detection(
            organization_id=organization_id,
            category=IssueCategory.EXCHANGE,
            detector_code="EXCHANGE_PENDING",
            dedupe_key=f"EXCHANGE_PENDING:{case.id}",
            title="Exchange request awaiting resolution",
            evidence={
                "return_case_id": str(case.id),
                "order_id": str(case.order_id),
                "status": case.status.value,
            },
            order_id=case.order_id,
            customer_id=case.customer_id,
            recommended_action="Confirm replacement stock is available and ship the exchange.",
            severity=IssueSeverity.MEDIUM,
        )
        for case in rows
    ]


ALL_DETECTORS: tuple[Any, ...] = (
    detect_unfulfilled_orders,
    detect_high_value_orders,
    detect_duplicate_orders,
    detect_partial_fulfillment_stuck,
    detect_payment_issues,
    detect_shipment_delayed,
    detect_tracking_missing,
    detect_ndr,
    detect_rto,
    detect_repeated_rto_customers,
    detect_out_of_stock,
    detect_low_stock,
    detect_dead_stock,
    detect_return_pending,
    detect_pickup_failed,
    detect_refund_pending,
    detect_exchange_pending,
)


def run_all(
    db: Session, organization_id: uuid.UUID, *, now: datetime | None = None
) -> list[Detection]:
    now = now or datetime.now(UTC)
    out: list[Detection] = []
    for detector in ALL_DETECTORS:
        out.extend(detector(db, organization_id, now))
    return out
