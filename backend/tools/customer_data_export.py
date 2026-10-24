"""Export what BizFlow stores about ONE customer, to answer a Shopify ``customers/data_request``.

Shopify's mandatory privacy webhooks (https://shopify.dev/docs/apps/build/compliance/
privacy-law-compliance) require an app to act on a customer's data request "within 30 days". BizFlow
acknowledges the webhook and records it in the ledger (identifiers only); this is the procedure that
fulfils it.

    cd backend
    python -m tools.customer_data_export --shop store.myshopify.com --customer-id 1234567890 \
        --out request-1234567890.json

``--customer-id`` is the numeric id in the webhook's ``customer.id``. The JSON written to ``--out``
contains the customer's stored fields and their orders and order lines. IT CONTAINS PERSONAL DATA:
the file is created owner-readable only, is never overwritten without ``--force``, is the only place
the data goes (the console shows counts, never values), and must be handled and deleted under your
data-protection policy once delivered to the merchant.

Only rows of the organization(s) that own that shop are read; nothing else in the database is
touched, and nothing is written to the database.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db.database import SessionLocal
from app.db.models import Customer, Order, OrderItem, ShopifyConnection
from app.db.scoping import scoped_select
from app.services.integrations.shopify.auth import SessionFactory
from app.services.integrations.shopify.validation import InvalidShopDomain, normalize_shop_domain


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value) if hasattr(value, "hex") and not isinstance(value, str) else value


def _row(obj: Any, columns: Sequence[str]) -> dict[str, Any]:
    return {name: _plain(getattr(obj, name)) for name in columns}


CUSTOMER_COLUMNS = (
    "shopify_customer_id",
    "email",
    "phone",
    "first_name",
    "last_name",
    "accepts_marketing",
    "shopify_created_at",
    "shopify_updated_at",
    "deleted_at",
    "synced_at",
)
ORDER_COLUMNS = (
    "shopify_order_id",
    "order_number",
    "financial_status",
    "fulfillment_status",
    "currency",
    "subtotal",
    "total",
    "created_at_shopify",
    "updated_at_shopify",
    "cancelled_at",
)
ITEM_COLUMNS = (
    "shopify_line_item_id",
    "shopify_product_id",
    "shopify_variant_id",
    "title",
    "sku",
    "quantity",
    "price",
)


def export_customer(
    session_factory: SessionFactory, shop: str, customer_id: int
) -> dict[str, Any] | None:
    """Everything stored about the customer under the organization(s) that own ``shop``."""
    with session_factory() as db:
        org_ids = list(
            dict.fromkeys(
                db.scalars(
                    select(ShopifyConnection.organization_id).where(
                        ShopifyConnection.shop_domain == shop
                    )
                )
            )
        )
        records = []
        for org_id in org_ids:
            customer = db.scalar(
                scoped_select(Customer, org_id).where(Customer.shopify_customer_id == customer_id)
            )
            if customer is None:
                continue
            orders = db.scalars(
                scoped_select(Order, org_id)
                .where(Order.customer_id == customer.id)
                .order_by(Order.shopify_order_id)
            ).all()
            records.append(
                {
                    "organization_id": str(org_id),
                    "customer": _row(customer, CUSTOMER_COLUMNS),
                    "orders": [
                        {
                            **_row(order, ORDER_COLUMNS),
                            "items": [
                                _row(item, ITEM_COLUMNS)
                                for item in db.scalars(
                                    scoped_select(OrderItem, org_id)
                                    .where(OrderItem.order_id == order.id)
                                    .order_by(OrderItem.shopify_line_item_id)
                                )
                            ],
                        }
                        for order in orders
                    ],
                }
            )
    if not records:
        return None
    return {
        "exported_at": datetime.now(UTC).isoformat(),
        "shop_domain": shop,
        "records": records,
    }


def write_private(path: Path, payload: dict[str, Any], *, force: bool) -> None:
    """Create the file readable by its owner only; refuse to clobber unless told to."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def main(argv: Sequence[str] | None = None, session_factory: SessionFactory = SessionLocal) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--shop", required=True, help="<name>.myshopify.com from the webhook")
    parser.add_argument("--customer-id", required=True, type=int, help="customer.id in the webhook")
    parser.add_argument("--out", required=True, type=Path, help="where to write the JSON")
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = parser.parse_args(argv)
    try:
        shop = normalize_shop_domain(args.shop)
    except InvalidShopDomain:
        print("FAIL  the shop must be <name>.myshopify.com")
        return 2
    data = export_customer(session_factory, shop, args.customer_id)
    if data is None:
        print(
            "INFO  nothing is stored for that customer (never imported, already redacted, or the "
            "store was disconnected and purged): reply to Shopify with 200 and note the date"
        )
        return 1
    try:
        write_private(args.out, data, force=args.force)
    except FileExistsError:
        print(f"FAIL  {args.out} already exists (use --force to overwrite)")
        return 2
    orders = sum(len(r["orders"]) for r in data["records"])
    items = sum(len(o["items"]) for r in data["records"] for o in r["orders"])
    print(
        f"OK    wrote {args.out}: {len(data['records'])} organization(s), {orders} order(s), "
        f"{items} line item(s). The file contains personal data: handle and delete it per policy."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
