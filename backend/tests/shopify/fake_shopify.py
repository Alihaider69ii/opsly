"""An in-memory fake of the parts of Shopify BizFlow talks to.

It answers the real wire formats: the OAuth token endpoint (authorization code and refresh token
grants with expiring, rotating tokens) and the GraphQL Admin API (shop, products with nested
variants, customers, orders with nested line items, webhook subscriptions) with cursor pagination
and the ``extensions.cost`` throttle metadata. Failure modes are switches: throttling, HTTP 429/5xx,
access denied, protected-data restriction, a revoked token.

``handler`` is an ``httpx`` mock-transport handler, so unit tests need no network; the browser
end-to-end script also serves it over a real local socket. Nothing here is used by production code.
"""

import json
import re
from typing import Any
from urllib.parse import parse_qs

import httpx

PAGE_COST = 100


def gid(kind: str, number: int) -> str:
    return f"gid://shopify/{kind}/{number}"


def make_variant(
    number: int,
    *,
    title: str = "Default Title",
    price: str = "19.99",
    sku: str | None = None,
    inventory: int | None = 5,
    compare_at: str | None = None,
    available: bool = True,
    updated: str = "2026-10-01T10:00:00Z",
) -> dict[str, Any]:
    return {
        "id": gid("ProductVariant", number),
        "title": title,
        "sku": sku or f"SKU-{number}",
        "price": price,
        "compareAtPrice": compare_at,
        "inventoryQuantity": inventory,
        "availableForSale": available,
        "createdAt": "2026-09-01T10:00:00Z",
        "updatedAt": updated,
    }


def make_product(
    number: int,
    *,
    title: str | None = None,
    variants: list[dict[str, Any]] | None = None,
    status: str = "ACTIVE",
    tags: list[str] | None = None,
    updated: str = "2026-10-01T10:00:00Z",
) -> dict[str, Any]:
    return {
        "id": gid("Product", number),
        "title": title or f"Product {number}",
        "handle": f"product-{number}",
        "description": f"Description of product {number}",
        "vendor": "Acme",
        "productType": "Gadget",
        "status": status,
        "tags": tags if tags is not None else ["new", "sale"],
        "onlineStoreUrl": f"https://shop.example/products/product-{number}",
        "createdAt": "2026-09-01T10:00:00Z",
        "updatedAt": updated,
        "_variants": variants if variants is not None else [make_variant(number * 100 + 1)],
    }


def make_customer(
    number: int,
    *,
    email: str | None = None,
    phone: str | None = None,
    first: str = "Ada",
    last: str = "Lovelace",
    marketing: str = "SUBSCRIBED",
    updated: str = "2026-10-01T10:00:00Z",
) -> dict[str, Any]:
    return {
        "id": gid("Customer", number),
        "firstName": first,
        "lastName": last,
        "createdAt": "2026-09-01T10:00:00Z",
        "updatedAt": updated,
        "state": "ENABLED",
        "defaultEmailAddress": {
            "emailAddress": email or f"customer{number}@example.com",
            "marketingState": marketing,
        },
        "defaultPhoneNumber": {"phoneNumber": phone or f"+1555000{number:04d}"},
    }


def make_line_item(
    number: int,
    *,
    product: int | None,
    variant: int | None,
    quantity: int = 1,
    price: str = "19.99",
) -> dict[str, Any]:
    return {
        "id": gid("LineItem", number),
        "title": f"Item {number}",
        "name": f"Item {number} - Default",
        "sku": f"SKU-{number}",
        "quantity": quantity,
        "originalUnitPriceSet": {"shopMoney": {"amount": price, "currencyCode": "USD"}},
        "product": {"id": gid("Product", product)} if product else None,
        "variant": {"id": gid("ProductVariant", variant)} if variant else None,
    }


def make_order(
    number: int,
    *,
    customer: int | None = None,
    items: list[dict[str, Any]] | None = None,
    total: str = "39.98",
    cancelled_at: str | None = None,
    updated: str = "2026-10-02T10:00:00Z",
) -> dict[str, Any]:
    return {
        "id": gid("Order", number),
        "name": f"#{1000 + number}",
        "displayFinancialStatus": "PAID",
        "displayFulfillmentStatus": "UNFULFILLED",
        "cancelledAt": cancelled_at,
        "createdAt": "2026-10-01T10:00:00Z",
        "updatedAt": updated,
        "currencyCode": "USD",
        "subtotalPriceSet": {"shopMoney": {"amount": "35.00", "currencyCode": "USD"}},
        "totalPriceSet": {"shopMoney": {"amount": total, "currencyCode": "USD"}},
        "customer": {"id": gid("Customer", customer)} if customer else None,
        "_items": items if items is not None else [],
    }


class FakeShopify:
    def __init__(
        self,
        *,
        shop: str = "test-shop.myshopify.com",
        client_id: str = "test-client-id",
        client_secret: str = "test-client-secret",
        scopes: str = "read_products,read_customers,read_orders",
    ) -> None:
        self.shop = shop
        self.client_id = client_id
        self.client_secret = client_secret
        self.granted_scopes = scopes
        self.shop_name = "Test Shop"
        self.currency = "USD"
        self.timezone = "America/New_York"
        self.products: list[dict[str, Any]] = []
        self.customers: list[dict[str, Any]] = []
        self.orders: list[dict[str, Any]] = []
        self.subscriptions: list[dict[str, str]] = []
        self.refuse_webhook_topics: set[str] = set()
        # token state
        self.valid_codes: set[str] = {"good-code"}
        self.expiring = True
        self.access_ttl = 3600
        self._counter = 0
        self.valid_access: set[str] = set()
        self.valid_refresh: set[str] = set()
        # new refresh token -> the refresh token it replaced (still usable during the grace period)
        self.refresh_parent: dict[str, str] = {}
        # failure switches
        self.throttle_responses = 0  # next N GraphQL calls answer THROTTLED
        self.http_failures: list[int] = []  # next GraphQL calls answer these statuses
        self.retry_after: str | None = None
        self.deny: set[str] = set()  # "products" | "customers" | "orders": ACCESS_DENIED, no data
        self.restrict_customer_pii = False  # data present, PII null, ACCESS_DENIED error
        self.token_endpoint_status: int | None = None
        self.bucket_available = 1000.0
        # observability
        self.requests: list[dict[str, Any]] = []
        self.token_requests: list[dict[str, str]] = []

    # -- tokens ----------------------------------------------------------------------------------

    def _issue(self) -> dict[str, Any]:
        self._counter += 1
        access, refresh = f"shpat_{self._counter}", f"shprt_{self._counter}"
        self.valid_access.add(access)
        self.valid_refresh.add(refresh)
        body: dict[str, Any] = {"access_token": access, "scope": self.granted_scopes}
        if self.expiring:
            body.update(
                expires_in=self.access_ttl,
                refresh_token=refresh,
                refresh_token_expires_in=7_776_000,
            )
        return body

    def revoke_all_tokens(self) -> None:
        self.valid_access.clear()
        self.valid_refresh.clear()
        self.refresh_parent.clear()

    def expire_refresh_grace(self) -> None:
        """30 days after a refresh token was first used to refresh, Shopify stops accepting it."""
        for parent in set(self.refresh_parent.values()):
            self.valid_refresh.discard(parent)

    def _retire_replaced_by(self, token: str) -> None:
        """Using a refresh token retires the ones it replaced (Shopify: "your app uses the newer
        refresh token... the normal case")."""
        parent = self.refresh_parent.pop(token, None)
        while parent is not None:
            self.valid_refresh.discard(parent)
            parent = self.refresh_parent.pop(parent, None)

    def _token_endpoint(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_requests.append(form)
        if self.token_endpoint_status:
            return httpx.Response(self.token_endpoint_status, json={"error": "boom"})
        if (
            form.get("client_id") != self.client_id
            or form.get("client_secret") != self.client_secret
        ):
            return httpx.Response(400, json={"error": "invalid_client"})
        if form.get("grant_type") == "refresh_token":
            old = form.get("refresh_token", "")
            if old not in self.valid_refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
            # Shopify's documented rotation: every refresh returns a new access AND refresh token,
            # but the presented token "stays usable until the earliest of": the newer token is used,
            # a new token is acquired by exchange, 30 days after it was first used, or its 90-day
            # expiry. So a refresh whose response was lost can be repeated with the old token.
            self._retire_replaced_by(old)
            body = self._issue()
            if "refresh_token" in body:
                self.refresh_parent[body["refresh_token"]] = old
            return httpx.Response(200, json=body)
        code = form.get("code", "")
        if code not in self.valid_codes:
            return httpx.Response(400, json={"error": "invalid_request"})
        self.valid_codes.discard(code)  # single use
        self.valid_refresh.clear()  # a freshly acquired token retires every earlier refresh token
        self.refresh_parent.clear()
        return httpx.Response(200, json=self._issue())

    # -- transport entry point -----------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/oauth/access_token":
            return self._token_endpoint(request)
        if re.fullmatch(r"/admin/api/[\d-]+/graphql\.json", request.url.path):
            return self._graphql(request)
        return httpx.Response(404, json={"errors": "not found"})

    # -- GraphQL ---------------------------------------------------------------------------------

    def _extensions(self) -> dict[str, Any]:
        available = self.bucket_available
        return {
            "cost": {
                "requestedQueryCost": PAGE_COST,
                "actualQueryCost": PAGE_COST,
                "throttleStatus": {
                    "maximumAvailable": 1000.0,
                    "currentlyAvailable": available,
                    "restoreRate": 50.0,
                },
            }
        }

    @staticmethod
    def _page(
        items: list[Any], first: int, after: str | None
    ) -> tuple[list[Any], bool, str | None]:
        start = int(after) + 1 if after is not None else 0
        chunk = items[start : start + first]
        has_next = start + first < len(items)
        end = str(start + len(chunk) - 1) if chunk else None
        return chunk, has_next, end

    def _connection(
        self, items: list[Any], first: int, after: str | None, render: Any
    ) -> dict[str, Any]:
        chunk, has_next, end = self._page(items, first, after)
        return {
            "pageInfo": {"hasNextPage": has_next, "endCursor": end},
            "nodes": [render(i) for i in chunk],
        }

    def _product_node(self, product: dict[str, Any], variants_first: int) -> dict[str, Any]:
        node = {k: v for k, v in product.items() if not k.startswith("_")}
        node["variants"] = self._connection(
            product.get("_variants", []), variants_first, None, lambda v: v
        )
        return node

    def _order_node(self, order: dict[str, Any], items_first: int) -> dict[str, Any]:
        node = {k: v for k, v in order.items() if not k.startswith("_")}
        node["lineItems"] = self._connection(
            order.get("_items", []), items_first, None, lambda i: i
        )
        return node

    def _customer_node(self, customer: dict[str, Any]) -> dict[str, Any]:
        if not self.restrict_customer_pii:
            return customer
        node = dict(customer)
        node.update(
            firstName=None, lastName=None, defaultEmailAddress=None, defaultPhoneNumber=None
        )
        return node

    def _find(self, items: list[dict[str, Any]], node_id: str) -> dict[str, Any] | None:
        return next((i for i in items if i["id"] == node_id), None)

    def _graphql(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query: str = body["query"]
        variables: dict[str, Any] = body.get("variables") or {}
        match = re.search(r"(?:query|mutation)\s+(\w+)", query)
        operation = match.group(1) if match else ""
        token = request.headers.get("x-shopify-access-token", "")
        self.requests.append({"operation": operation, "variables": variables, "token": token})

        if self.http_failures:
            status = self.http_failures.pop(0)
            headers = {"Retry-After": self.retry_after} if self.retry_after else {}
            return httpx.Response(status, headers=headers, json={"errors": "failure"})
        if token not in self.valid_access:
            return httpx.Response(401, json={"errors": "[API] Invalid API key or access token"})
        if self.throttle_responses > 0:
            self.throttle_responses -= 1
            return httpx.Response(
                200,
                json={
                    "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                    "extensions": self._extensions(),
                },
            )

        data, errors = self._execute(operation, variables)
        payload: dict[str, Any] = {"data": data, "extensions": self._extensions()}
        if errors:
            payload["errors"] = errors
        return httpx.Response(200, json=payload)

    def _denied(self) -> list[dict[str, Any]]:
        return [
            {
                "message": "This app is not approved to access the Customer object.",
                "extensions": {"code": "ACCESS_DENIED"},
            }
        ]

    def _execute(
        self, operation: str, v: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
        if operation == "ShopInfo":
            return {
                "shop": {
                    "name": self.shop_name,
                    "myshopifyDomain": self.shop,
                    "currencyCode": self.currency,
                    "ianaTimezone": self.timezone,
                }
            }, None
        if operation == "Products":
            if "products" in self.deny:
                return {"products": None}, self._denied()
            return {
                "products": self._connection(
                    self.products,
                    v["first"],
                    v.get("after"),
                    lambda p: self._product_node(p, v["variants"]),
                )
            }, None
        if operation == "Product":
            product = self._find(self.products, v["id"])
            return {"node": self._product_node(product, v["variants"]) if product else None}, None
        if operation == "ProductVariants":
            product = self._find(self.products, v["id"])
            if product is None:
                return {"node": None}, None
            return {
                "node": {
                    "variants": self._connection(
                        product["_variants"], v["first"], v.get("after"), lambda x: x
                    )
                }
            }, None
        if operation == "Customers":
            if "customers" in self.deny:
                return {"customers": None}, self._denied()
            return {
                "customers": self._connection(
                    self.customers, v["first"], v.get("after"), self._customer_node
                )
            }, (self._denied() if self.restrict_customer_pii else None)
        if operation == "Customer":
            customer = self._find(self.customers, v["id"])
            return {"node": self._customer_node(customer) if customer else None}, None
        if operation == "Orders":
            if "orders" in self.deny:
                return {"orders": None}, self._denied()
            return {
                "orders": self._connection(
                    self.orders,
                    v["first"],
                    v.get("after"),
                    lambda o: self._order_node(o, v["items"]),
                )
            }, None
        if operation == "Order":
            order = self._find(self.orders, v["id"])
            return {"node": self._order_node(order, v["items"]) if order else None}, None
        if operation == "OrderLineItems":
            order = self._find(self.orders, v["id"])
            if order is None:
                return {"node": None}, None
            return {
                "node": {
                    "lineItems": self._connection(
                        order["_items"], v["first"], v.get("after"), lambda x: x
                    )
                }
            }, None
        if operation == "WebhookSubscriptions":
            return {
                "webhookSubscriptions": self._connection(
                    self.subscriptions, v["first"], v.get("after"), lambda s: s
                )
            }, None
        if operation == "WebhookCreate":
            if v["topic"] in self.refuse_webhook_topics:
                return {
                    "webhookSubscriptionCreate": {
                        "webhookSubscription": None,
                        "userErrors": [{"field": ["topic"], "message": "refused"}],
                    }
                }, None
            sub = {
                "id": gid("WebhookSubscription", len(self.subscriptions) + 1),
                "topic": v["topic"],
                "uri": v["uri"],
            }
            self.subscriptions.append(sub)
            return {
                "webhookSubscriptionCreate": {
                    "webhookSubscription": {"id": sub["id"]},
                    "userErrors": [],
                }
            }, None
        if operation == "WebhookDelete":
            before = len(self.subscriptions)
            self.subscriptions = [s for s in self.subscriptions if s["id"] != v["id"]]
            return {
                "webhookSubscriptionDelete": {
                    "deletedWebhookSubscriptionId": v["id"]
                    if len(self.subscriptions) < before
                    else None,
                    "userErrors": [],
                }
            }, None
        return {}, [
            {"message": f"unknown operation {operation}", "extensions": {"code": "UNKNOWN"}}
        ]

    # -- helpers for tests ---------------------------------------------------------------------

    def operations(self) -> list[str]:
        return [r["operation"] for r in self.requests]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
