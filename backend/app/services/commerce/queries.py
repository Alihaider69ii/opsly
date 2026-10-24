"""Read access to imported commerce data, for other BizFlow services (a later phase's AI agent, for
example). The only sanctioned way to read products, variants, customers and orders.

Every query starts from ``scoped_select`` with an organization id the *caller* has already verified,
so another organization's rows are unreachable by construction, including by a guessed or leaked id.
Nothing here is exposed over HTTP; customer fields are personal data and are returned only to
trusted internal callers, which must never log them.

This module is deliberately independent of Shopify: it knows BizFlow's tables, not Shopify's API.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.models import Customer, Order, OrderItem, Product, ProductVariant
from app.db.scoping import scoped_select

MAX_LIMIT = 100


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_products(
    db: Session,
    organization_id: uuid.UUID,
    *,
    search: str | None = None,
    status: str | None = "ACTIVE",
    limit: int = 20,
    offset: int = 0,
) -> Sequence[Product]:
    """Products that still exist in the store, newest first. ``status=None`` includes drafts and
    archived products; deleted products are never returned."""
    stmt = scoped_select(Product, organization_id).where(Product.deleted_at.is_(None))
    if status is not None:
        stmt = stmt.where(Product.status == status)
    if search and search.strip():
        pattern = f"%{_escape_like(search.strip().lower())}%"
        stmt = stmt.where(
            or_(
                Product.title.ilike(pattern, escape="\\"),
                Product.vendor.ilike(pattern, escape="\\"),
                Product.product_type.ilike(pattern, escape="\\"),
            )
        )
    stmt = (
        stmt.order_by(Product.shopify_updated_at.desc().nulls_last(), Product.id)
        .limit(max(1, min(limit, MAX_LIMIT)))
        .offset(max(0, offset))
    )
    return db.scalars(stmt).all()


def get_product(db: Session, organization_id: uuid.UUID, product_id: uuid.UUID) -> Product | None:
    return db.scalar(
        scoped_select(Product, organization_id).where(
            Product.id == product_id, Product.deleted_at.is_(None)
        )
    )


def list_variants(
    db: Session, organization_id: uuid.UUID, product_id: uuid.UUID
) -> Sequence[ProductVariant]:
    return db.scalars(
        scoped_select(ProductVariant, organization_id)
        .where(ProductVariant.product_id == product_id, ProductVariant.deleted_at.is_(None))
        .order_by(ProductVariant.price.asc().nulls_last(), ProductVariant.id)
    ).all()


def get_customer(
    db: Session, organization_id: uuid.UUID, customer_id: uuid.UUID
) -> Customer | None:
    return db.scalar(
        scoped_select(Customer, organization_id).where(
            Customer.id == customer_id, Customer.deleted_at.is_(None)
        )
    )


def get_order(db: Session, organization_id: uuid.UUID, order_id: uuid.UUID) -> Order | None:
    return db.scalar(scoped_select(Order, organization_id).where(Order.id == order_id))


def list_order_items(
    db: Session, organization_id: uuid.UUID, order_id: uuid.UUID
) -> Sequence[OrderItem]:
    return db.scalars(
        scoped_select(OrderItem, organization_id)
        .where(OrderItem.order_id == order_id)
        .order_by(OrderItem.shopify_line_item_id)
    ).all()


def orders_for_customer(
    db: Session, organization_id: uuid.UUID, customer_id: uuid.UUID, *, limit: int = 20
) -> Sequence[Order]:
    return db.scalars(
        scoped_select(Order, organization_id)
        .where(Order.customer_id == customer_id)
        .order_by(Order.created_at_shopify.desc().nulls_last(), Order.id)
        .limit(max(1, min(limit, MAX_LIMIT)))
    ).all()
