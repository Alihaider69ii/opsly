"""Persistence of imported commerce data. Every statement is scoped to one organization.

Writes are idempotent upserts keyed by ``(organization_id, shopify_*_id)``: re-running a sync, or a
webhook racing a sync, updates rows instead of duplicating them. Callers commit; each sync page is
one transaction.

Ordering safety: Shopify may deliver or return an older version of an object after a newer one was
stored. An incoming object whose Shopify ``updatedAt`` is older than the stored one is *stale*: its
fields are not written (only ``synced_at`` is refreshed, since the object was seen).

Deletions seen in Shopify mark a row with ``deleted_at`` rather than removing it, so orders keep
their history. ``purge_shop_data`` is the only function that deletes rows for good.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.db.models import Customer, Order, OrderItem, Product, ProductVariant
from app.db.upsert import upsert
from app.services.integrations.shopify.mapper import (
    CustomerData,
    OrderData,
    ProductData,
)

PURGE_BATCH = 5000


@dataclass(frozen=True)
class WriteResult:
    written: int
    stale: int


@dataclass(frozen=True)
class CommerceCounts:
    products: int
    variants: int
    customers: int
    orders: int


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC so comparisons are valid everywhere."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _is_stale(incoming: datetime | None, stored: datetime | None) -> bool:
    a, b = _aware(incoming), _aware(stored)
    return a is not None and b is not None and a < b


# --- products ------------------------------------------------------------------------------------


def upsert_products(
    db: Session,
    organization_id: uuid.UUID,
    products: Sequence[ProductData],
    *,
    currency: str | None,
    now: datetime,
) -> WriteResult:
    by_id = {p.shopify_product_id: p for p in products}
    if not by_id:
        return WriteResult(0, 0)
    stored = {
        shopify_id: updated
        for shopify_id, updated in db.execute(
            select(Product.shopify_product_id, Product.shopify_updated_at).where(
                Product.organization_id == organization_id,
                Product.shopify_product_id.in_(list(by_id)),
            )
        )
    }
    fresh = {
        sid: p for sid, p in by_id.items() if not _is_stale(p.shopify_updated_at, stored.get(sid))
    }
    stale_ids = [sid for sid in by_id if sid not in fresh]
    if stale_ids:
        db.execute(
            update(Product)
            .where(
                Product.organization_id == organization_id,
                Product.shopify_product_id.in_(stale_ids),
            )
            .values(synced_at=now)
        )

    upsert(
        db,
        Product,
        [
            {
                "id": uuid.uuid4(),
                "organization_id": organization_id,
                "shopify_product_id": p.shopify_product_id,
                "title": p.title,
                "handle": p.handle,
                "description": p.description,
                "vendor": p.vendor,
                "product_type": p.product_type,
                "status": p.status,
                "tags": p.tags,
                "product_url": p.product_url,
                "currency": currency,
                "shopify_created_at": p.shopify_created_at,
                "shopify_updated_at": p.shopify_updated_at,
                "deleted_at": None,
                "synced_at": now,
            }
            for p in fresh.values()
        ],
        conflict=("organization_id", "shopify_product_id"),
        update=(
            "title",
            "handle",
            "description",
            "vendor",
            "product_type",
            "status",
            "tags",
            "product_url",
            "currency",
            "shopify_created_at",
            "shopify_updated_at",
            "deleted_at",
            "synced_at",
        ),
    )
    local_ids = {
        sid: pid
        for sid, pid in db.execute(
            select(Product.shopify_product_id, Product.id).where(
                Product.organization_id == organization_id,
                Product.shopify_product_id.in_(list(fresh)),
            )
        )
    }

    variant_rows: dict[int, dict[str, object]] = {}
    for sid, product in fresh.items():
        for v in product.variants:
            variant_rows[v.shopify_variant_id] = {
                "id": uuid.uuid4(),
                "organization_id": organization_id,
                "product_id": local_ids[sid],
                "shopify_variant_id": v.shopify_variant_id,
                "title": v.title,
                "sku": v.sku,
                "price": v.price,
                "compare_at_price": v.compare_at_price,
                "currency": currency,
                "inventory_quantity": v.inventory_quantity,
                "available": v.available,
                "deleted_at": None,
                "synced_at": now,
            }
    upsert(
        db,
        ProductVariant,
        list(variant_rows.values()),
        conflict=("organization_id", "shopify_variant_id"),
        update=(
            "title",
            "sku",
            "price",
            "compare_at_price",
            "currency",
            "inventory_quantity",
            "available",
            "deleted_at",
            "synced_at",
        ),
    )
    for sid, product in fresh.items():
        if not product.variants_complete:
            continue  # a variant missing from a partial list may simply be on the next page
        keep = [v.shopify_variant_id for v in product.variants]
        stmt = update(ProductVariant).where(
            ProductVariant.organization_id == organization_id,
            ProductVariant.product_id == local_ids[sid],
            ProductVariant.deleted_at.is_(None),
        )
        if keep:
            stmt = stmt.where(ProductVariant.shopify_variant_id.not_in(keep))
        db.execute(stmt.values(deleted_at=now))
    return WriteResult(len(fresh), len(stale_ids))


def mark_product_deleted(
    db: Session, organization_id: uuid.UUID, shopify_product_id: int, now: datetime
) -> bool:
    """Record that Shopify deleted a product. The row and its history stay; it is just inactive."""
    product_ids = select(Product.id).where(
        Product.organization_id == organization_id,
        Product.shopify_product_id == shopify_product_id,
    )
    result = db.execute(
        update(Product)
        .where(Product.organization_id == organization_id, Product.id.in_(product_ids))
        .values(deleted_at=now)
    )
    db.execute(
        update(ProductVariant)
        .where(
            ProductVariant.organization_id == organization_id,
            ProductVariant.product_id.in_(product_ids),
            ProductVariant.deleted_at.is_(None),
        )
        .values(deleted_at=now)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


# --- customers -----------------------------------------------------------------------------------


def upsert_customers(
    db: Session,
    organization_id: uuid.UUID,
    customers: Sequence[CustomerData],
    *,
    now: datetime,
) -> WriteResult:
    by_id = {c.shopify_customer_id: c for c in customers}
    if not by_id:
        return WriteResult(0, 0)
    stored = {
        shopify_id: updated
        for shopify_id, updated in db.execute(
            select(Customer.shopify_customer_id, Customer.shopify_updated_at).where(
                Customer.organization_id == organization_id,
                Customer.shopify_customer_id.in_(list(by_id)),
            )
        )
    }
    fresh = {
        sid: c for sid, c in by_id.items() if not _is_stale(c.shopify_updated_at, stored.get(sid))
    }
    stale_ids = [sid for sid in by_id if sid not in fresh]
    if stale_ids:
        db.execute(
            update(Customer)
            .where(
                Customer.organization_id == organization_id,
                Customer.shopify_customer_id.in_(stale_ids),
            )
            .values(synced_at=now)
        )
    upsert(
        db,
        Customer,
        [
            {
                "id": uuid.uuid4(),
                "organization_id": organization_id,
                "shopify_customer_id": c.shopify_customer_id,
                "email": c.email,
                "phone": c.phone,
                "first_name": c.first_name,
                "last_name": c.last_name,
                "accepts_marketing": c.accepts_marketing,
                "tags": c.tags,
                "shopify_created_at": c.shopify_created_at,
                "shopify_updated_at": c.shopify_updated_at,
                "deleted_at": None,
                "synced_at": now,
            }
            for c in fresh.values()
        ],
        conflict=("organization_id", "shopify_customer_id"),
        update=(
            "email",
            "phone",
            "first_name",
            "last_name",
            "accepts_marketing",
            "tags",
            "shopify_created_at",
            "shopify_updated_at",
            "deleted_at",
            "synced_at",
        ),
    )
    return WriteResult(len(fresh), len(stale_ids))


def mark_customer_deleted(
    db: Session, organization_id: uuid.UUID, shopify_customer_id: int, now: datetime
) -> bool:
    result = db.execute(
        update(Customer)
        .where(
            Customer.organization_id == organization_id,
            Customer.shopify_customer_id == shopify_customer_id,
        )
        .values(deleted_at=now)
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


def anonymize_customer(
    db: Session, organization_id: uuid.UUID, shopify_customer_id: int, now: datetime
) -> bool:
    """Erase a customer's personal data (a GDPR ``customers/redact`` request). The row survives so
    orders keep their reference, but nothing identifying remains."""
    result = db.execute(
        update(Customer)
        .where(
            Customer.organization_id == organization_id,
            Customer.shopify_customer_id == shopify_customer_id,
        )
        .values(
            email=None,
            phone=None,
            first_name=None,
            last_name=None,
            accepts_marketing=None,
            tags=[],
            deleted_at=now,
        )
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


# --- orders --------------------------------------------------------------------------------------


def upsert_orders(
    db: Session,
    organization_id: uuid.UUID,
    orders: Sequence[OrderData],
    *,
    now: datetime,
) -> WriteResult:
    by_id = {o.shopify_order_id: o for o in orders}
    if not by_id:
        return WriteResult(0, 0)
    stored = {
        shopify_id: updated
        for shopify_id, updated in db.execute(
            select(Order.shopify_order_id, Order.updated_at_shopify).where(
                Order.organization_id == organization_id,
                Order.shopify_order_id.in_(list(by_id)),
            )
        )
    }
    fresh = {
        sid: o for sid, o in by_id.items() if not _is_stale(o.updated_at_shopify, stored.get(sid))
    }
    stale_ids = [sid for sid in by_id if sid not in fresh]
    if stale_ids:
        db.execute(
            update(Order)
            .where(Order.organization_id == organization_id, Order.shopify_order_id.in_(stale_ids))
            .values(synced_at=now)
        )

    customer_ids = {o.shopify_customer_id for o in fresh.values() if o.shopify_customer_id}
    customers = (
        {
            sid: cid
            for sid, cid in db.execute(
                select(Customer.shopify_customer_id, Customer.id).where(
                    Customer.organization_id == organization_id,
                    Customer.shopify_customer_id.in_(customer_ids),
                )
            )
        }
        if customer_ids
        else {}
    )
    upsert(
        db,
        Order,
        [
            {
                "id": uuid.uuid4(),
                "organization_id": organization_id,
                "shopify_order_id": o.shopify_order_id,
                "order_number": o.order_number,
                "customer_id": customers.get(o.shopify_customer_id)
                if o.shopify_customer_id
                else None,
                "financial_status": o.financial_status,
                "fulfillment_status": o.fulfillment_status,
                "currency": o.currency,
                "subtotal": o.subtotal,
                "total": o.total,
                "created_at_shopify": o.created_at_shopify,
                "updated_at_shopify": o.updated_at_shopify,
                "cancelled_at": o.cancelled_at,
                "synced_at": now,
            }
            for o in fresh.values()
        ],
        conflict=("organization_id", "shopify_order_id"),
        update=(
            "order_number",
            "customer_id",
            "financial_status",
            "fulfillment_status",
            "currency",
            "subtotal",
            "total",
            "created_at_shopify",
            "updated_at_shopify",
            "cancelled_at",
            "synced_at",
        ),
    )
    order_ids = {
        sid: oid
        for sid, oid in db.execute(
            select(Order.shopify_order_id, Order.id).where(
                Order.organization_id == organization_id,
                Order.shopify_order_id.in_(list(fresh)),
            )
        )
    }

    product_refs = {
        i.shopify_product_id for o in fresh.values() for i in o.line_items if i.shopify_product_id
    }
    variant_refs = {
        i.shopify_variant_id for o in fresh.values() for i in o.line_items if i.shopify_variant_id
    }
    products = (
        {
            sid: pid
            for sid, pid in db.execute(
                select(Product.shopify_product_id, Product.id).where(
                    Product.organization_id == organization_id,
                    Product.shopify_product_id.in_(product_refs),
                )
            )
        }
        if product_refs
        else {}
    )
    variants = (
        {
            sid: vid
            for sid, vid in db.execute(
                select(ProductVariant.shopify_variant_id, ProductVariant.id).where(
                    ProductVariant.organization_id == organization_id,
                    ProductVariant.shopify_variant_id.in_(variant_refs),
                )
            )
        }
        if variant_refs
        else {}
    )
    item_rows: dict[int, dict[str, object]] = {}
    for sid, order in fresh.items():
        for item in order.line_items:
            item_rows[item.shopify_line_item_id] = {
                "id": uuid.uuid4(),
                "organization_id": organization_id,
                "order_id": order_ids[sid],
                "product_id": products.get(item.shopify_product_id)
                if item.shopify_product_id
                else None,
                "variant_id": variants.get(item.shopify_variant_id)
                if item.shopify_variant_id
                else None,
                "shopify_line_item_id": item.shopify_line_item_id,
                "shopify_product_id": item.shopify_product_id,
                "shopify_variant_id": item.shopify_variant_id,
                "title": item.title,
                "sku": item.sku,
                "quantity": item.quantity,
                "price": item.price,
            }
    upsert(
        db,
        OrderItem,
        list(item_rows.values()),
        conflict=("organization_id", "shopify_line_item_id"),
        update=(
            "product_id",
            "variant_id",
            "shopify_product_id",
            "shopify_variant_id",
            "title",
            "sku",
            "quantity",
            "price",
        ),
        touch_updated_at=False,
    )
    for sid, order in fresh.items():
        if not order.items_complete:
            continue
        keep = [i.shopify_line_item_id for i in order.line_items]
        stmt = delete(OrderItem).where(
            OrderItem.organization_id == organization_id, OrderItem.order_id == order_ids[sid]
        )
        if keep:
            stmt = stmt.where(OrderItem.shopify_line_item_id.not_in(keep))
        db.execute(stmt)  # a line item removed by an order edit is removed here too
    return WriteResult(len(fresh), len(stale_ids))


# --- reconciliation, counts, purge ---------------------------------------------------------------


def soft_delete_unseen(
    db: Session, organization_id: uuid.UUID, *, products: bool, customers: bool, since: datetime
) -> dict[str, int]:
    """After a *complete* import, mark rows the import did not see (``synced_at`` older than the run
    start) as deleted. This repairs a missed delete webhook. It must only run when the whole
    resource was read successfully."""
    removed = {"products": 0, "customers": 0}
    if products:
        result = db.execute(
            update(Product)
            .where(
                Product.organization_id == organization_id,
                Product.deleted_at.is_(None),
                Product.synced_at < since,
            )
            .values(deleted_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        removed["products"] = int(result.rowcount or 0)  # type: ignore[attr-defined]
    if customers:
        result = db.execute(
            update(Customer)
            .where(
                Customer.organization_id == organization_id,
                Customer.deleted_at.is_(None),
                Customer.synced_at.is_not(None),
                Customer.synced_at < since,
            )
            .values(deleted_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        removed["customers"] = int(result.rowcount or 0)  # type: ignore[attr-defined]
    return removed


def commerce_counts(db: Session, organization_id: uuid.UUID) -> CommerceCounts:
    def count(model: type[Product] | type[ProductVariant] | type[Customer] | type[Order]) -> int:
        stmt = (
            select(func.count()).select_from(model).where(model.organization_id == organization_id)
        )
        if hasattr(model, "deleted_at"):
            stmt = stmt.where(model.deleted_at.is_(None))
        return int(db.scalar(stmt) or 0)

    return CommerceCounts(
        products=count(Product),
        variants=count(ProductVariant),
        customers=count(Customer),
        orders=count(Order),
    )


def purge_shop_data(
    db: Session, organization_id: uuid.UUID, *, batch: int = PURGE_BATCH
) -> dict[str, int]:
    """Permanently delete every imported commerce row of the organization, children first.

    Used when a store is disconnected or a ``shop/redact`` request arrives. Everything is
    re-importable from Shopify. Deletes in batches and commits per batch so locks stay short.
    """
    removed: dict[str, int] = {}
    for name, model in (
        ("order_items", OrderItem),
        ("orders", Order),
        ("customers", Customer),
        ("product_variants", ProductVariant),
        ("products", Product),
    ):
        total = 0
        while True:
            ids = select(model.id).where(model.organization_id == organization_id).limit(batch)
            result = db.execute(
                delete(model).where(model.organization_id == organization_id, model.id.in_(ids))
            )
            db.commit()
            deleted = int(result.rowcount or 0)  # type: ignore[attr-defined]
            total += deleted
            if deleted < batch:
                break
        removed[name] = total
    return removed
