"""Every Shopify access scope BizFlow asks for, and why. The only place scope strings are written.

Principle: request the minimum that Phase 4 needs, nothing administrative, nothing that writes.
(Write scopes would also grant the matching read scope, so BizFlow asks for read scopes only.)

This module imports nothing from the application so that configuration can read the defaults.
"""

from dataclasses import dataclass

READ_PRODUCTS = "read_products"
READ_CUSTOMERS = "read_customers"
READ_ORDERS = "read_orders"


@dataclass(frozen=True)
class ScopeInfo:
    scope: str
    required: bool
    why: str


SCOPES: tuple[ScopeInfo, ...] = (
    ScopeInfo(
        READ_PRODUCTS,
        required=True,
        why=(
            "Import the product catalog and variants (titles, prices, inventory, availability), "
            "and receive products/* webhooks. The catalog is the one thing the integration cannot "
            "work without, so connecting fails if this scope is declined."
        ),
    ),
    ScopeInfo(
        READ_CUSTOMERS,
        required=False,
        why=(
            "Import customer records (name, email, phone, marketing consent) and receive "
            "customers/* webhooks. Optional: without it the customer import is skipped."
        ),
    ),
    ScopeInfo(
        READ_ORDERS,
        required=False,
        why=(
            "Import orders and their line items for the last 60 days (Shopify's default window) "
            "and receive orders/* webhooks. Optional: without it the order import is skipped. "
            "read_all_orders (history older than 60 days) is deliberately NOT requested; it needs "
            "separate approval from Shopify and is not needed yet."
        ),
    ),
)

DEFAULT_SCOPES: tuple[str, ...] = tuple(info.scope for info in SCOPES)
REQUIRED_SCOPES: frozenset[str] = frozenset(info.scope for info in SCOPES if info.required)

# Webhook topic (GraphQL enum name) -> the scope that must have been granted to subscribe to it.
# An empty string means the topic needs no scope.
WEBHOOK_TOPIC_SCOPES: dict[str, str] = {
    "PRODUCTS_CREATE": READ_PRODUCTS,
    "PRODUCTS_UPDATE": READ_PRODUCTS,
    "PRODUCTS_DELETE": READ_PRODUCTS,
    "CUSTOMERS_CREATE": READ_CUSTOMERS,
    "CUSTOMERS_UPDATE": READ_CUSTOMERS,
    "CUSTOMERS_DELETE": READ_CUSTOMERS,
    "ORDERS_CREATE": READ_ORDERS,
    "ORDERS_UPDATED": READ_ORDERS,
    "ORDERS_CANCELLED": READ_ORDERS,
    "APP_UNINSTALLED": "",
}


def parse_scopes(raw: str | None) -> frozenset[str]:
    """Scopes arrive as a comma-separated string ("read_products,read_orders")."""
    return frozenset(part.strip() for part in (raw or "").split(",") if part.strip())


def scope_string(scopes: frozenset[str] | set[str] | tuple[str, ...]) -> str:
    return ",".join(sorted(scopes))


def covers(granted: frozenset[str], scope: str) -> bool:
    """Whether ``scope`` is granted. A write scope implies its read scope (Shopify's rule)."""
    if not scope:
        return True
    if scope in granted:
        return True
    return scope.startswith("read_") and ("write_" + scope.removeprefix("read_")) in granted
