"""Verify BizFlow's Shopify integration against a REAL store, read-only, and report without PII.

Prerequisites (see docs/shopify.md, "Live verification runbook"): a Shopify app registered in the
Dev Dashboard, ``SHOPIFY_CLIENT_ID`` / ``SHOPIFY_CLIENT_SECRET`` / ``SECRET_ENCRYPTION_KEY`` set, a
development store, and the store CONNECTED through the dashboard (the OAuth consent screen needs a
human; it is deliberately not automated).

    cd backend
    python -m tools.shopify_live_check --shop your-dev-store.myshopify.com
    python -m tools.shopify_live_check --shop ... --pages 3 --webhooks
    python -m tools.shopify_live_check --shop ... --refresh   # rotate the tokens, prove they work

What it does (it never writes to Shopify and never changes BizFlow's data):

  * finds the connection in the database and decrypts its token IN MEMORY (never printed);
  * reads the shop (name, currency, time zone) and reports the Admin API version Shopify actually
    served against the configured one (a mismatch means the version was retired and fell forward);
  * reads one page (or ``--pages N`` pages, to prove cursors) of products, customers and orders and
    prints COUNTS ONLY: objects, variants, line items, malformed objects, whether Shopify withheld
    protected customer data, how many customers came back with an email / phone (a count, not the
    values);
  * reports the GraphQL cost bucket Shopify last described (throttling);
  * ``--webhooks``: lists BizFlow's subscriptions at Shopify against the topics it needs;
  * ``--refresh``: forces a token refresh (rotates both tokens; Shopify keeps the old refresh token
    usable for a grace period) and confirms the new access token works.

Output contains identifiers and numbers only: no token, no customer name, email, phone or address.
Exit status is non-zero if any check fails. Findings that are limitations of the store or app
approval rather than bugs (for example protected customer data not approved) are reported as WARN.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.secrets import SecretError, SecretManager
from app.db.database import SessionLocal
from app.db.models import ShopifyConnection, ShopifyConnectionStatus
from app.services.integrations.shopify import fetch
from app.services.integrations.shopify import scopes as scope_rules
from app.services.integrations.shopify.auth import SessionFactory
from app.services.integrations.shopify.client import ShopifyClient
from app.services.integrations.shopify.errors import ShopifyAccessDenied, ShopifyError
from app.services.integrations.shopify.factory import ShopifyRuntime, check_api_version
from app.services.integrations.shopify.lifecycle import webhook_url
from app.services.integrations.shopify.validation import InvalidShopDomain, normalize_shop_domain
from app.services.integrations.shopify.webhooks import API_TOPICS, graphql_topic

FAILURES: list[str] = []


def report(ok: bool, name: str, detail: str = "") -> None:
    if not ok:
        FAILURES.append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def warn(name: str, detail: str) -> None:
    print(f"WARN  {name}   [{detail}]")


def info(name: str, detail: str) -> None:
    print(f"INFO  {name}   [{detail}]")


def find_connection(session_factory: SessionFactory, shop: str) -> ShopifyConnection | None:
    with session_factory() as db:
        return db.scalar(
            select(ShopifyConnection)
            .where(
                ShopifyConnection.shop_domain == shop,
                ShopifyConnection.status != ShopifyConnectionStatus.DISCONNECTED,
            )
            .order_by(ShopifyConnection.updated_at.desc())
        )


def check_shop(client: ShopifyClient, settings: Settings) -> None:
    try:
        shop = client.shop_info()
    except ShopifyError as exc:
        report(False, "read the shop", f"code={exc.code}")
        return
    report(True, "read the shop", f"name={shop.name!r} currency={shop.currency} tz={shop.timezone}")
    served = client.served_version
    if served is None:
        info("Admin API version served", "Shopify sent no X-Shopify-API-Version header")
    else:
        report(
            served == settings.shopify_api_version,
            "Shopify served the configured API version",
            f"configured={settings.shopify_api_version} served={served}",
        )
    info("configured version status", check_api_version(settings.shopify_api_version))


def check_resource(name: str, client: ShopifyClient, pages: int, granted: frozenset[str]) -> None:
    pagers: dict[str, Callable[[ShopifyClient, str | None], fetch.Page[Any]]] = {
        "products": fetch.fetch_products_page,
        "customers": fetch.fetch_customers_page,
        "orders": fetch.fetch_orders_page,
    }
    pager = pagers[name]
    needed = {"products": "read_products", "customers": "read_customers", "orders": "read_orders"}
    if not scope_rules.covers(granted, needed[name]):
        warn(name, f"scope {needed[name]} was not granted, so BizFlow correctly skips this import")
        return
    cursor: str | None = None
    totals = {"objects": 0, "nested": 0, "malformed": 0, "with_email": 0, "with_phone": 0}
    restricted = False
    seen_pages = 0
    try:
        for index in range(1, pages + 1):
            page = pager(client, cursor)
            seen_pages += 1
            restricted = restricted or page.restricted
            totals["objects"] += len(page.items)
            totals["malformed"] += page.malformed
            for item in page.items:  # type: Any
                if name == "products":
                    totals["nested"] += len(item.variants)
                elif name == "orders":
                    totals["nested"] += len(item.line_items)
                else:
                    totals["with_email"] += 1 if item.email else 0
                    totals["with_phone"] += 1 if item.phone else 0
            print(
                f"INFO  {name} page {index}   [objects={len(page.items)} has_next={page.has_next} "
                f"malformed={page.malformed}]"
            )
            if not page.has_next:
                break
            cursor = page.next_cursor
    except ShopifyAccessDenied:
        warn(name, "Shopify denied access (missing scope or protected customer data approval)")
        return
    except ShopifyError as exc:
        report(False, f"read {name}", f"code={exc.code}")
        return
    report(totals["malformed"] == 0, f"read {name}", f"pages={seen_pages} " + str(totals))
    if restricted:
        warn(
            name,
            "Shopify withheld protected customer data (ACCESS_DENIED): the import keeps what it "
            "got; request protected customer data access in the Dev/Partner Dashboard",
        )
    if name == "customers" and totals["objects"] and totals["with_email"] == 0:
        warn(
            name, "no customer returned an email: protected customer data is probably not approved"
        )


def check_webhooks(client: ShopifyClient, settings: Settings, granted: frozenset[str]) -> None:
    if not settings.shopify_webhook_base_url:
        warn("webhooks", "SHOPIFY_WEBHOOK_BASE_URL is not set, so none are expected")
        return
    uri = webhook_url(settings.shopify_webhook_base_url)
    try:
        present = {s["topic"] for s in client.list_webhook_subscriptions() if s["uri"] == uri}
    except ShopifyError as exc:
        report(False, "list webhook subscriptions", f"code={exc.code}")
        return
    wanted = {
        graphql_topic(t)
        for t in API_TOPICS
        if scope_rules.covers(granted, scope_rules.WEBHOOK_TOPIC_SCOPES[graphql_topic(t)])
    }
    missing = sorted(wanted - present)
    report(not missing, "all webhook subscriptions exist", f"{len(present)} of {len(wanted)}")
    if missing:
        info("missing topics", ", ".join(missing))
    info(
        "compliance webhooks",
        "customers/data_request, customers/redact and shop/redact are "
        "configured in the app settings in Shopify, not through the API: confirm them there",
    )


def check_refresh(rt: ShopifyRuntime, session_factory: SessionFactory, connection_id, shop: str):
    tokens = rt.tokens_for(session_factory, connection_id)
    try:
        tokens.refresh(force=True)
    except ShopifyError as exc:
        report(False, "token refresh", f"code={exc.code}")
        return
    with session_factory() as db:
        row = db.get(ShopifyConnection, connection_id)
        expires = row.access_token_expires_at if row else None
    report(True, "token refresh rotated and stored both tokens", f"access expires {expires}")
    client = rt.authorized_client(session_factory, connection_id, shop)
    try:
        client.shop_info()
        report(True, "the new access token works")
    except ShopifyError as exc:
        report(False, "the new access token works", f"code={exc.code}")
    finally:
        client.close()


def run(
    args: argparse.Namespace,
    settings: Settings,
    session_factory: SessionFactory,
    rt: ShopifyRuntime | None = None,
) -> int:
    try:
        shop = normalize_shop_domain(args.shop)
    except InvalidShopDomain:
        report(False, "the shop must be <name>.myshopify.com")
        return 1
    if not settings.shopify_configured:
        report(False, "Shopify is configured", "set SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET")
        return 1
    if rt is None:
        try:
            rt = ShopifyRuntime(settings=settings, secrets=SecretManager.from_settings(settings))
        except SecretError as exc:
            report(False, "secure storage is usable", f"code={exc.code}")
            return 1
    connection = find_connection(session_factory, shop)
    if connection is None:
        report(False, "the store is connected in BizFlow", "connect it from the dashboard first")
        return 1
    granted = scope_rules.parse_scopes(connection.scopes)
    report(
        True, "found the connection", f"org={connection.organization_id} status={connection.status}"
    )
    info("granted scopes", ", ".join(sorted(granted)))
    info("webhook status", str(connection.webhook_status))
    now = datetime.now(UTC)
    for label, value in (
        ("access token expires", connection.access_token_expires_at),
        ("refresh token expires", connection.refresh_token_expires_at),
    ):
        if value is not None:
            when = value if value.tzinfo else value.replace(tzinfo=UTC)
            info(label, f"in {(when - now).total_seconds() / 3600:.1f} hours")

    client = rt.authorized_client(session_factory, connection.id, shop)
    try:
        check_shop(client, settings)
        for name in ("products", "customers", "orders"):
            check_resource(name, client, args.pages, granted)
        bucket = client.governor.snapshot()
        info(
            "GraphQL cost bucket",
            "no cost reported"
            if bucket["maximum"] is None
            else f"{bucket['available']:.0f} of {bucket['maximum']:.0f} points, "
            f"restoring {bucket['restore_rate']:.0f}/s",
        )
        if args.webhooks:
            check_webhooks(client, settings, granted)
    except ShopifyError as exc:
        report(False, "talk to Shopify", f"code={exc.code}")
    finally:
        client.close()
    if args.refresh:
        check_refresh(rt, session_factory, connection.id, shop)
    return 1 if FAILURES else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--shop", required=True, help="the connected <name>.myshopify.com")
    parser.add_argument("--pages", type=int, default=1, help="pages per resource (default 1)")
    parser.add_argument("--webhooks", action="store_true", help="check webhook subscriptions")
    parser.add_argument("--refresh", action="store_true", help="force a token refresh")
    args = parser.parse_args(argv)
    code = run(args, get_settings(), SessionLocal)
    print("\nALL CHECKS PASSED" if code == 0 else f"\n{len(FAILURES)} CHECK(S) FAILED")
    return code


if __name__ == "__main__":
    sys.exit(main())
