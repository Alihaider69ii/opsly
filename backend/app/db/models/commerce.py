"""Normalized commerce data (products, variants, customers, orders), imported from Shopify today.

BizFlow owns these rows: each has its own UUID primary key, and Shopify ids are only unique-per-
organization attributes (``organization_id`` + ``shopify_*_id``). Every table carries
``organization_id`` and children point at their parent through a composite ``(id, organization_id)``
foreign key, so a variant, order item or order can never reference another tenant's row.

Customer fields are personal data. They are never returned by a public endpoint and never logged.
Deletions seen in Shopify are recorded with ``deleted_at`` instead of removing the row, so business
history (orders and their items) keeps its references.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin

MONEY = Numeric(18, 4)


class Product(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_products_id_organization_id"),
        UniqueConstraint(
            "organization_id", "shopify_product_id", name="uq_products_org_shopify_product"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    shopify_product_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(500))
    handle: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    vendor: Mapped[str | None] = mapped_column(String(255))
    product_type: Mapped[str | None] = mapped_column(String(255))
    # Shopify's ACTIVE | DRAFT | ARCHIVED | UNLISTED (text, so a new value cannot break a sync).
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    product_url: Mapped[str | None] = mapped_column(String(2048))
    currency: Mapped[str | None] = mapped_column(String(10))
    shopify_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shopify_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProductVariant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "product_variants"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_product_variants_id_organization_id"),
        UniqueConstraint(
            "organization_id", "shopify_variant_id", name="uq_product_variants_org_shopify_variant"
        ),
        ForeignKeyConstraint(
            ["product_id", "organization_id"],
            ["products.id", "products.organization_id"],
            ondelete="CASCADE",
            name="fk_product_variants_product_org",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    shopify_variant_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(500))
    sku: Mapped[str | None] = mapped_column(String(255))
    price: Mapped[Decimal | None] = mapped_column(MONEY)
    compare_at_price: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str | None] = mapped_column(String(10))
    # Not every store tracks inventory the same way; unknown stays NULL rather than 0.
    inventory_quantity: Mapped[int | None] = mapped_column(Integer)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Customer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A customer. Email and phone may each be absent (Shopify stores allow both)."""

    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_customers_id_organization_id"),
        # shopify_customer_id is NULL for customers that did not come from Shopify (later phases);
        # NULLs never collide in a unique constraint.
        UniqueConstraint(
            "organization_id", "shopify_customer_id", name="uq_customers_org_shopify_customer"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    shopify_customer_id: Mapped[int | None] = mapped_column(BigInteger)
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(50))
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    accepts_marketing: Mapped[bool | None] = mapped_column(Boolean)
    shopify_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shopify_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Order(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_orders_id_organization_id"),
        UniqueConstraint("organization_id", "shopify_order_id", name="uq_orders_org_shopify_order"),
        ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            name="fk_orders_customer_org",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    shopify_order_id: Mapped[int] = mapped_column(BigInteger)
    order_number: Mapped[str | None] = mapped_column(String(50))  # the display name, e.g. "#1001"
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    financial_status: Mapped[str | None] = mapped_column(String(40))
    fulfillment_status: Mapped[str | None] = mapped_column(String(40))
    currency: Mapped[str | None] = mapped_column(String(10))
    subtotal: Mapped[Decimal | None] = mapped_column(MONEY)
    total: Mapped[Decimal | None] = mapped_column(MONEY)
    created_at_shopify: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at_shopify: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrderItem(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "order_items"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "shopify_line_item_id", name="uq_order_items_org_shopify_line_item"
        ),
        ForeignKeyConstraint(
            ["order_id", "organization_id"],
            ["orders.id", "orders.organization_id"],
            ondelete="CASCADE",
            name="fk_order_items_order_org",
        ),
        ForeignKeyConstraint(
            ["product_id", "organization_id"],
            ["products.id", "products.organization_id"],
            name="fk_order_items_product_org",
        ),
        ForeignKeyConstraint(
            ["variant_id", "organization_id"],
            ["product_variants.id", "product_variants.organization_id"],
            name="fk_order_items_variant_org",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    variant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    shopify_line_item_id: Mapped[int] = mapped_column(BigInteger)
    # Kept so an item can be linked later if its product was not yet (or no longer) synced.
    shopify_product_id: Mapped[int | None] = mapped_column(BigInteger)
    shopify_variant_id: Mapped[int | None] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(500))
    sku: Mapped[str | None] = mapped_column(String(255))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal | None] = mapped_column(MONEY)
