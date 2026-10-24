"""Reading products, customers and orders from Shopify as normalized data.

Used by both the full sync (page by page) and the webhook processor (one object by id), so there is
exactly one place that knows the GraphQL shapes and how to finish an object whose nested rows span
several pages. No database access happens here.
"""

from dataclasses import dataclass, field, replace
from typing import Any

from app.services.integrations.shopify import mapper, queries
from app.services.integrations.shopify.client import GraphQLResult, ShopifyClient
from app.services.integrations.shopify.mapper import CustomerData, OrderData, ProductData

# Shopify allows up to 2048 variants per product; this bounds a misbehaving response, not a store.
MAX_NESTED_PAGES = 40


@dataclass
class Page[T]:
    items: list[T] = field(default_factory=list)
    malformed: int = 0
    has_next: bool = False
    next_cursor: str | None = None
    # True when Shopify withheld some fields (protected customer data) but returned the objects.
    restricted: bool = False


def _gid(kind: str, number: int) -> str:
    return f"gid://shopify/{kind}/{number}"


def _connection(result: GraphQLResult, key: str) -> Any:
    return result.data.get(key) or {}


# --- products ------------------------------------------------------------------------------------


def _complete_variants(client: ShopifyClient, product: ProductData) -> ProductData:
    """Fetch the variants that did not fit in the first page and return the complete product."""
    variants = list(product.variants)
    cursor = product.variants_cursor
    for _ in range(MAX_NESTED_PAGES):
        result = client.graphql(
            queries.PRODUCT_VARIANTS_MORE,
            {
                "id": _gid("Product", product.shopify_product_id),
                "first": queries.NESTED_FOLLOW_UP_SIZE,
                "after": cursor,
            },
            operation="ProductVariants",
            cost_hint=queries.COST_HINTS["nested"],
        )
        node = result.data.get("node") or {}
        connection = node.get("variants")
        more = [v for v in (mapper.map_variant(n) for n in mapper.page_nodes(connection)) if v]
        variants.extend(more)
        has_next, cursor = mapper.page_info(connection)
        if not has_next:
            break
    return replace(product, variants=variants, variants_complete=True, variants_cursor=None)


def fetch_products_page(client: ShopifyClient, cursor: str | None) -> Page[ProductData]:
    result = client.graphql(
        queries.PRODUCTS_PAGE,
        {
            "first": queries.PRODUCT_PAGE_SIZE,
            "after": cursor,
            "variants": queries.VARIANT_PAGE_SIZE,
        },
        operation="Products",
        cost_hint=queries.COST_HINTS["products"],
    )
    connection = _connection(result, "products")
    page: Page[ProductData] = Page(restricted=result.access_denied)
    for node in mapper.page_nodes(connection):
        product = mapper.map_product(node)
        if product is None:
            page.malformed += 1
            continue
        if not product.variants_complete:
            product = _complete_variants(client, product)
        page.items.append(product)
    page.has_next, page.next_cursor = mapper.page_info(connection)
    return page


def fetch_product(client: ShopifyClient, shopify_product_id: int) -> ProductData | None:
    """The current product, or None if it no longer exists (or is not a product)."""
    result = client.graphql(
        queries.PRODUCT_BY_ID,
        {"id": _gid("Product", shopify_product_id), "variants": queries.VARIANT_PAGE_SIZE},
        operation="Product",
        cost_hint=queries.COST_HINTS["products"],
    )
    product = mapper.map_product(result.data.get("node"))
    if product is not None and not product.variants_complete:
        product = _complete_variants(client, product)
    return product


# --- customers -----------------------------------------------------------------------------------


def fetch_customers_page(client: ShopifyClient, cursor: str | None) -> Page[CustomerData]:
    result = client.graphql(
        queries.CUSTOMERS_PAGE,
        {"first": queries.CUSTOMER_PAGE_SIZE, "after": cursor},
        operation="Customers",
        cost_hint=queries.COST_HINTS["customers"],
    )
    connection = _connection(result, "customers")
    page: Page[CustomerData] = Page(restricted=result.access_denied)
    for node in mapper.page_nodes(connection):
        customer = mapper.map_customer(node)
        if customer is None:
            page.malformed += 1
        else:
            page.items.append(customer)
    page.has_next, page.next_cursor = mapper.page_info(connection)
    return page


def fetch_customer(client: ShopifyClient, shopify_customer_id: int) -> CustomerData | None:
    result = client.graphql(
        queries.CUSTOMER_BY_ID,
        {"id": _gid("Customer", shopify_customer_id)},
        operation="Customer",
        cost_hint=queries.COST_HINTS["small"],
    )
    return mapper.map_customer(result.data.get("node"))


# --- orders --------------------------------------------------------------------------------------


def _complete_line_items(client: ShopifyClient, order: OrderData) -> OrderData:
    items = list(order.line_items)
    cursor = order.items_cursor
    for _ in range(MAX_NESTED_PAGES):
        result = client.graphql(
            queries.ORDER_LINE_ITEMS_MORE,
            {
                "id": _gid("Order", order.shopify_order_id),
                "first": queries.NESTED_FOLLOW_UP_SIZE,
                "after": cursor,
            },
            operation="OrderLineItems",
            cost_hint=queries.COST_HINTS["nested"],
        )
        node = result.data.get("node") or {}
        connection = node.get("lineItems")
        more = [i for i in (mapper.map_line_item(n) for n in mapper.page_nodes(connection)) if i]
        items.extend(more)
        has_next, cursor = mapper.page_info(connection)
        if not has_next:
            break
    return replace(order, line_items=items, items_complete=True, items_cursor=None)


def fetch_orders_page(client: ShopifyClient, cursor: str | None) -> Page[OrderData]:
    result = client.graphql(
        queries.ORDERS_PAGE,
        {"first": queries.ORDER_PAGE_SIZE, "after": cursor, "items": queries.LINE_ITEM_PAGE_SIZE},
        operation="Orders",
        cost_hint=queries.COST_HINTS["orders"],
    )
    connection = _connection(result, "orders")
    page: Page[OrderData] = Page(restricted=result.access_denied)
    for node in mapper.page_nodes(connection):
        order = mapper.map_order(node)
        if order is None:
            page.malformed += 1
            continue
        if not order.items_complete:
            order = _complete_line_items(client, order)
        page.items.append(order)
    page.has_next, page.next_cursor = mapper.page_info(connection)
    return page


def fetch_order(client: ShopifyClient, shopify_order_id: int) -> OrderData | None:
    result = client.graphql(
        queries.ORDER_BY_ID,
        {"id": _gid("Order", shopify_order_id), "items": queries.LINE_ITEM_PAGE_SIZE},
        operation="Order",
        cost_hint=queries.COST_HINTS["orders"],
    )
    order = mapper.map_order(result.data.get("node"))
    if order is not None and not order.items_complete:
        order = _complete_line_items(client, order)
    return order
