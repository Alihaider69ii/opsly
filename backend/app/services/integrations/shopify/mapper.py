"""Deterministic, pure mappers from Shopify GraphQL nodes to BizFlow's normalized data.

Nothing here touches the database or the network, so every mapping is unit-testable with plain
dicts. Shopify's payloads vary by store and by permission (a field may be missing, null, or
withheld because the app is not approved for protected customer data), so each mapper tolerates
absent fields and returns ``None`` for an object it cannot identify. The caller counts those and
carries on: one malformed object never fails a whole sync.

Only the fields BizFlow needs are kept. Raw Shopify JSON is never stored.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.integrations.shopify.validation import parse_gid

MAX_TAGS = 250
MAX_DESCRIPTION_CHARS = 20_000
MAX_MONEY = Decimal("100000000000")  # 1e11: far above any real price, below the column's range


@dataclass(frozen=True)
class VariantData:
    shopify_variant_id: int
    title: str
    sku: str | None
    price: Decimal | None
    compare_at_price: Decimal | None
    inventory_quantity: int | None
    available: bool


@dataclass(frozen=True)
class ProductData:
    shopify_product_id: int
    title: str
    handle: str | None
    description: str | None
    vendor: str | None
    product_type: str | None
    status: str
    tags: list[str]
    product_url: str | None
    shopify_created_at: datetime | None
    shopify_updated_at: datetime | None
    variants: list[VariantData] = field(default_factory=list)
    # False when Shopify reported more variants than the page returned (the caller fetches the rest
    # before treating missing variants as removed).
    variants_complete: bool = True
    variants_cursor: str | None = None
    malformed_variants: int = 0


@dataclass(frozen=True)
class CustomerData:
    shopify_customer_id: int
    email: str | None
    phone: str | None
    first_name: str | None
    last_name: str | None
    accepts_marketing: bool | None
    shopify_created_at: datetime | None
    shopify_updated_at: datetime | None


@dataclass(frozen=True)
class LineItemData:
    shopify_line_item_id: int
    shopify_product_id: int | None
    shopify_variant_id: int | None
    title: str
    sku: str | None
    quantity: int
    price: Decimal | None


@dataclass(frozen=True)
class OrderData:
    shopify_order_id: int
    order_number: str | None
    shopify_customer_id: int | None
    financial_status: str | None
    fulfillment_status: str | None
    currency: str | None
    subtotal: Decimal | None
    total: Decimal | None
    created_at_shopify: datetime | None
    updated_at_shopify: datetime | None
    cancelled_at: datetime | None
    line_items: list[LineItemData] = field(default_factory=list)
    items_complete: bool = True
    items_cursor: str | None = None
    malformed_items: int = 0


# --- scalar helpers ------------------------------------------------------------------------------


def text(value: Any, max_chars: int = 255) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch == "\n" or ch >= " ").strip()
    return cleaned[:max_chars] if cleaned else None


def money(value: Any) -> Decimal | None:
    """A Money scalar arrives as a decimal string ("12.50"). Anything else, NaN, infinity or an
    absurd magnitude becomes None."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not amount.is_finite() or abs(amount) >= MAX_MONEY:
        return None
    return amount


def integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit() and len(value) <= 18:
        return int(value)
    return None


def timestamp(value: Any) -> datetime | None:
    """ISO 8601 (``2026-10-21T10:00:00Z``) as an aware UTC datetime, or None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _nodes(connection: Any) -> list[Any]:
    if isinstance(connection, Mapping):
        nodes = connection.get("nodes")
        if isinstance(nodes, list):
            return nodes
    return []


def _page_info(connection: Any) -> tuple[bool, str | None]:
    info = connection.get("pageInfo") if isinstance(connection, Mapping) else None
    if not isinstance(info, Mapping):
        return False, None
    cursor = info.get("endCursor")
    return bool(info.get("hasNextPage")), cursor if isinstance(cursor, str) else None


def _shop_money(price_set: Any) -> tuple[Decimal | None, str | None]:
    shop_money = price_set.get("shopMoney") if isinstance(price_set, Mapping) else None
    if not isinstance(shop_money, Mapping):
        return None, None
    return money(shop_money.get("amount")), text(shop_money.get("currencyCode"), 10)


def _web_url(value: Any) -> str | None:
    url = text(value, 2048)
    return url if url and url.lower().startswith(("https://", "http://")) else None


# --- products ------------------------------------------------------------------------------------


def map_variant(node: Any) -> VariantData | None:
    if not isinstance(node, Mapping):
        return None
    variant_id = parse_gid(node.get("id"), "ProductVariant")
    if variant_id is None:
        return None
    available = node.get("availableForSale")
    return VariantData(
        shopify_variant_id=variant_id,
        title=text(node.get("title"), 500) or "Default",
        sku=text(node.get("sku")),
        price=money(node.get("price")),
        compare_at_price=money(node.get("compareAtPrice")),
        inventory_quantity=integer(node.get("inventoryQuantity")),
        available=available if isinstance(available, bool) else True,
    )


def map_product(node: Any) -> ProductData | None:
    if not isinstance(node, Mapping):
        return None
    product_id = parse_gid(node.get("id"), "Product")
    title = text(node.get("title"), 500)
    if product_id is None or title is None:
        return None

    variant_nodes = _nodes(node.get("variants"))
    variants = [v for v in (map_variant(n) for n in variant_nodes) if v is not None]
    has_more, cursor = _page_info(node.get("variants"))

    raw_tags = node.get("tags")
    tags = (
        [t for t in (text(tag) for tag in raw_tags[:MAX_TAGS]) if t]
        if isinstance(raw_tags, Sequence) and not isinstance(raw_tags, str)
        else []
    )
    status = text(node.get("status"), 30)
    return ProductData(
        shopify_product_id=product_id,
        title=title,
        handle=text(node.get("handle")),
        description=text(node.get("description"), MAX_DESCRIPTION_CHARS),
        vendor=text(node.get("vendor")),
        product_type=text(node.get("productType")),
        status=status.upper() if status else "ACTIVE",
        tags=tags,
        product_url=_web_url(node.get("onlineStoreUrl")),
        shopify_created_at=timestamp(node.get("createdAt")),
        shopify_updated_at=timestamp(node.get("updatedAt")),
        variants=variants,
        variants_complete=not has_more,
        variants_cursor=cursor if has_more else None,
        malformed_variants=len(variant_nodes) - len(variants),
    )


# --- customers -----------------------------------------------------------------------------------


def _accepts_marketing(email_address: Any) -> bool | None:
    state = email_address.get("marketingState") if isinstance(email_address, Mapping) else None
    if not isinstance(state, str):
        return None  # withheld or absent: unknown, not "no"
    return state.upper() == "SUBSCRIBED"


def map_customer(node: Any) -> CustomerData | None:
    if not isinstance(node, Mapping):
        return None
    customer_id = parse_gid(node.get("id"), "Customer")
    if customer_id is None:
        return None
    email = node.get("defaultEmailAddress")
    phone = node.get("defaultPhoneNumber")
    return CustomerData(
        shopify_customer_id=customer_id,
        email=text(email.get("emailAddress"), 320) if isinstance(email, Mapping) else None,
        phone=text(phone.get("phoneNumber"), 50) if isinstance(phone, Mapping) else None,
        first_name=text(node.get("firstName")),
        last_name=text(node.get("lastName")),
        accepts_marketing=_accepts_marketing(email),
        shopify_created_at=timestamp(node.get("createdAt")),
        shopify_updated_at=timestamp(node.get("updatedAt")),
    )


# --- orders --------------------------------------------------------------------------------------


def map_line_item(node: Any) -> LineItemData | None:
    if not isinstance(node, Mapping):
        return None
    item_id = parse_gid(node.get("id"), "LineItem")
    quantity = integer(node.get("quantity"))
    if item_id is None or quantity is None or quantity < 0:
        return None
    product = node.get("product")
    variant = node.get("variant")
    price, _ = _shop_money(node.get("originalUnitPriceSet"))
    return LineItemData(
        shopify_line_item_id=item_id,
        shopify_product_id=parse_gid(product.get("id"), "Product")
        if isinstance(product, Mapping)
        else None,
        shopify_variant_id=parse_gid(variant.get("id"), "ProductVariant")
        if isinstance(variant, Mapping)
        else None,
        title=text(node.get("title"), 500) or text(node.get("name"), 500) or "Item",
        sku=text(node.get("sku")),
        quantity=quantity,
        price=price,
    )


def map_order(node: Any) -> OrderData | None:
    if not isinstance(node, Mapping):
        return None
    order_id = parse_gid(node.get("id"), "Order")
    if order_id is None:
        return None
    subtotal, _ = _shop_money(node.get("subtotalPriceSet"))
    total, total_currency = _shop_money(node.get("totalPriceSet"))
    customer = node.get("customer")
    item_nodes = _nodes(node.get("lineItems"))
    items = [i for i in (map_line_item(n) for n in item_nodes) if i is not None]
    has_more, cursor = _page_info(node.get("lineItems"))
    financial = text(node.get("displayFinancialStatus"), 40)
    fulfillment = text(node.get("displayFulfillmentStatus"), 40)
    return OrderData(
        shopify_order_id=order_id,
        order_number=text(node.get("name"), 50),
        shopify_customer_id=parse_gid(customer.get("id"), "Customer")
        if isinstance(customer, Mapping)
        else None,
        financial_status=financial.upper() if financial else None,
        fulfillment_status=fulfillment.upper() if fulfillment else None,
        currency=total_currency or text(node.get("currencyCode"), 10),
        subtotal=subtotal,
        total=total,
        created_at_shopify=timestamp(node.get("createdAt")),
        updated_at_shopify=timestamp(node.get("updatedAt")),
        cancelled_at=timestamp(node.get("cancelledAt")),
        line_items=items,
        items_complete=not has_more,
        items_cursor=cursor if has_more else None,
        malformed_items=len(item_nodes) - len(items),
    )


def page_nodes(connection: Any) -> list[Any]:
    """The nodes of a top-level connection (public so the sync can iterate pages)."""
    return _nodes(connection)


def page_info(connection: Any) -> tuple[bool, str | None]:
    return _page_info(connection)
