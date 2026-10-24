"""Mutants for the Phase 4 controls (tenant filtering, OAuth, token protection, webhooks, roles, sync
claiming, embedding-model separation, access-log redaction). Each entry changes one thing in
production code; the listed tests must then fail. See README.md.
"""

SERVICE = "app/services/integrations/shopify/"

UNIT = ["tests/shopify", "tests/core/test_secrets.py"]
ALL_UNIT = ["tests"]
PG = ["-m", "integration", "tests/integration/test_shopify_postgres.py"]
PG_RACE = [*PG, "-k", "race or simultaneous or exactly_once or racing or two_workers"]
EMB = ["-m", "integration", "tests/integration/test_embedding_models.py"]
MIG = ["-m", "integration", "tests/integration/test_migrations.py"]

# (id, file, old, new, pytest args)
M = []


def add(mid, file, old, new, args=UNIT):
    M.append((mid, file, old, new, args))


# ---- tenant filtering ------------------------------------------------------------------------------
add(
    "org-01-scoped_select",
    "app/db/scoping.py",
    "    return select(model).where(column == organization_id)",
    "    return select(model).where(column == column)",
    ALL_UNIT,
)
add(
    "org-02-connection-lookup",
    SERVICE + "service.py",
    "    return db.scalar(scoped_select(ShopifyConnection, ctx.organization.id))",
    "    return db.scalar(scoped_select(ShopifyConnection, ShopifyConnection.organization_id))",
)
add(
    "org-03-sync-claim-org-check",
    SERVICE + "service.py",
    "            ShopifyConnection.organization_id == org_id,\n            ShopifyConnection.status == ShopifyConnectionStatus.CONNECTED,",
    "            ShopifyConnection.status == ShopifyConnectionStatus.CONNECTED,",
)
add(
    "org-04-get_product",
    "app/services/commerce/queries.py",
    "        scoped_select(Product, organization_id).where(",
    "        scoped_select(Product, Product.organization_id).where(",
)
add(
    "org-05-get_order",
    "app/services/commerce/queries.py",
    "scoped_select(Order, organization_id).where(Order.id == order_id)",
    "scoped_select(Order, Order.organization_id).where(Order.id == order_id)",
)
add(
    "org-06-get_customer",
    "app/services/commerce/queries.py",
    "        scoped_select(Customer, organization_id).where(",
    "        scoped_select(Customer, Customer.organization_id).where(",
)
add(
    "org-07-list_variants",
    "app/services/commerce/queries.py",
    "        scoped_select(ProductVariant, organization_id)\n",
    "        scoped_select(ProductVariant, ProductVariant.organization_id)\n",
)
add(
    "org-08-counts",
    SERVICE + "store.py",
    "            select(func.count()).select_from(model).where(model.organization_id == organization_id)",
    "            select(func.count()).select_from(model)",
)
add(
    "org-09-purge-ids",
    SERVICE + "store.py",
    "            ids = select(model.id).where(model.organization_id == organization_id).limit(batch)",
    "            ids = select(model.id).limit(batch)",
)
add(
    "org-10-purge-delete",
    SERVICE + "store.py",
    "delete(model).where(model.organization_id == organization_id, model.id.in_(ids))",
    "delete(model).where(model.id.in_(ids))",
)
add(
    "org-11-upsert-stale-lookup",
    SERVICE + "store.py",
    "                Product.organization_id == organization_id,\n                Product.shopify_product_id.in_(list(by_id)),",
    "                Product.shopify_product_id.in_(list(by_id)),",
)
add(
    "org-12-reconcile",
    SERVICE + "store.py",
    "                Product.organization_id == organization_id,\n                Product.deleted_at.is_(None),\n                Product.synced_at < since,",
    "                Product.deleted_at.is_(None),\n                Product.synced_at < since,",
)
add(
    "org-13-webhook-routing",
    SERVICE + "webhooks.py",
    "            ShopifyConnection.shop_domain == shop,\n            ShopifyConnection.status != ShopifyConnectionStatus.DISCONNECTED,",
    "            ShopifyConnection.status != ShopifyConnectionStatus.DISCONNECTED,",
)

# ---- OAuth ---------------------------------------------------------------------------------------------
add(
    "oauth-01-replay",
    SERVICE + "oauth.py",
    "    if row is None or row.consumed_at is not None:",
    "    if row is None:",
)
add(
    "oauth-02-expiry",
    SERVICE + "oauth.py",
    "    if _aware(row.expires_at) <= now:",
    "    if False:",
)
add(
    "oauth-03-binding-org-and-user",
    SERVICE + "oauth.py",
    "    if row.organization_id != organization_id or row.user_id != user_id:",
    "    if False:",
)
add(
    "oauth-04-binding-user-only",
    SERVICE + "oauth.py",
    "    if row.organization_id != organization_id or row.user_id != user_id:",
    "    if row.organization_id != organization_id:",
)
add(
    "oauth-05-binding-org-only",
    SERVICE + "oauth.py",
    "    if row.organization_id != organization_id or row.user_id != user_id:",
    "    if row.user_id != user_id:",
)
add(
    "oauth-06-shop-bound",
    SERVICE + "oauth.py",
    "    if row.shop_domain != shop:",
    "    if False:",
)
add(
    "oauth-07-callback-hmac",
    SERVICE + "oauth.py",
    "    if not verify_oauth_hmac(query, _client_secret(settings)):",
    "    if False:",
)
add(
    "oauth-08-callback-timestamp",
    SERVICE + "oauth.py",
    "    if now - issued > CALLBACK_MAX_AGE or issued - now > CALLBACK_MAX_FUTURE_SKEW:",
    "    if False:",
)
add(
    "oauth-09-atomic-consume",
    SERVICE + "oauth.py",
    "            ShopifyOAuthState.consumed_at.is_(None),\n            ShopifyOAuthState.expires_at > now,",
    "            ShopifyOAuthState.expires_at > now,",
    # only a real race on PostgreSQL exposes a consume that is not atomic
    [*PG, "-k", "simultaneous_callbacks"],
)
add(
    "oauth-10-duplicate-params",
    SERVICE + "validation.py",
    "        if key in result:",
    "        if False:",
)
add(
    "oauth-11-hmac-compare",
    SERVICE + "validation.py",
    "    return _constant_time_equal(expected, provided.lower())",
    "    return True",
)
add(
    "oauth-12-state-stored-per-user",
    SERVICE + "oauth.py",
    "            ShopifyOAuthState.user_id == user_id,\n            ShopifyOAuthState.consumed_at.is_(None),",
    "            ShopifyOAuthState.consumed_at.is_(None),",
)

# ---- shop domain / SSRF ------------------------------------------------------------------------------------
add(
    "domain-01-suffix",
    SERVICE + "validation.py",
    '        raise InvalidShopDomain("not a myshopify.com store domain")',
    "        pass",
)
add(
    "domain-02-scheme-and-credentials",
    SERVICE + "validation.py",
    '        if parts.scheme not in ("http", "https") or parts.username or parts.password:',
    "        if False:",
)
add(
    "domain-03-port-query",
    SERVICE + "validation.py",
    "        if parts.port is not None or parts.query or parts.fragment:",
    "        if False:",
)
add(
    "domain-04-path",
    SERVICE + "validation.py",
    '        elif parts.path not in ("", "/"):',
    "        elif False:",
)
add(
    "domain-05-ascii",
    SERVICE + "validation.py",
    "    if not text or len(text) > 255 or not text.isascii():",
    "    if not text or len(text) > 255:",
)

# ---- token protection ---------------------------------------------------------------------------------------
add(
    "token-01-access-plaintext",
    SERVICE + "auth.py",
    '    connection.access_token_encrypted = secrets.encrypt(\n        grant.access_token, context=token_context("access_token", connection.id)\n    )',
    "    connection.access_token_encrypted = grant.access_token",
)
add(
    "token-02-refresh-plaintext",
    SERVICE + "auth.py",
    '        connection.refresh_token_encrypted = secrets.encrypt(\n            grant.refresh_token, context=token_context("refresh_token", connection.id)\n        )',
    "        connection.refresh_token_encrypted = grant.refresh_token",
)
add(
    "token-03-log-token",
    SERVICE + "auth.py",
    '    """Encrypt and store a token grant on the connection (the caller commits)."""',
    '    """Encrypt and store a token grant on the connection (the caller commits)."""\n    __import__("logging").getLogger(__name__).warning("grant %s", grant.access_token)',
)
add(
    "token-04-api-exposes-ciphertext",
    "app/schemas/shopify.py",
    "    shop_domain: str\n    shop_name: str | None",
    "    shop_domain: str\n    access_token_encrypted: str | None = None\n    shop_name: str | None",
)
add(
    "token-05-disconnect-keeps-tokens",
    SERVICE + "lifecycle.py",
    "        clear_tokens(connection)",
    "        pass",
)
add(
    "token-06-context-binding",
    "app/core/secrets.py",
    "        sealed = self._ciphers[self._current_id].encrypt(nonce, data, self._aad(context))",
    "        sealed = self._ciphers[self._current_id].encrypt(nonce, data, self._aad(''))",
)
add(
    "token-07-refresh-not-locked",
    SERVICE + "auth.py",
    "            stmt = stmt.with_for_update()",
    "            pass",
    [*PG, "-k", "rotate"],
)
add(
    "token-08-decrypt-error-leaks",
    SERVICE + "auth.py",
    "from app.core.secrets import",
    "import app.core.secrets as _s0  # noqa\nfrom app.core.secrets import",
)  # sanity mutant: harmless import, must SURVIVE (control)

# ---- webhook verification ------------------------------------------------------------------------------------
add(
    "hook-01-no-verification",
    SERVICE + "webhooks.py",
    '    if not verify_webhook_hmac(body, headers.get("x-shopify-hmac-sha256"), secret):',
    "    if False:",
)
add(
    "hook-02-compare-always-true",
    SERVICE + "validation.py",
    "    return hmac.compare_digest(expected, provided)",
    "    return True",
)
add(
    "hook-03-empty-secret-ok",
    SERVICE + "validation.py",
    "    if not header_value or not client_secret:\n        return False\n    try:\n        provided = base64",
    "    if not header_value:\n        return False\n    try:\n        provided = base64",
)
add(
    "hook-04-missing-header-ok",
    SERVICE + "validation.py",
    "    if not header_value or not client_secret:\n        return False\n    try:\n        provided = base64",
    "    if not client_secret:\n        return False\n    if not header_value:\n        return True\n    try:\n        provided = base64",
)
add(
    "hook-05-disconnected-store-accepted",
    SERVICE + "webhooks.py",
    "            ShopifyConnection.status != ShopifyConnectionStatus.DISCONNECTED,\n        )\n    )\n    if connection is None and topic in COMPLIANCE_TOPICS:",
    "        )\n    )\n    if connection is None and topic in COMPLIANCE_TOPICS:",
)
add(
    "hook-06-shop-header-unvalidated",
    SERVICE + "webhooks.py",
    '    except InvalidShopDomain:\n        raise WebhookRejected(400, "invalid shop domain") from None',
    "    except InvalidShopDomain:\n        shop = (headers.get('x-shopify-shop-domain') or '').lower()",
)
add(
    "hook-07-body-size-cap",
    "app/api/webhooks/shopify.py",
    "        if len(body) > limit:",
    "        if False:",
)
add(
    "hook-08-bad-signature-limiter",
    "app/api/webhooks/shopify.py",
    "        if exc.status_code == 401 and not limiter.allow(",
    "        if False and not limiter.allow(",
)

# ---- webhook deduplication -----------------------------------------------------------------------------------
add(
    "dedup-01-precheck",
    SERVICE + "webhooks.py",
    "    if db.scalar(\n        select(ShopifyWebhookEvent.id).where(",
    "    if False and db.scalar(\n        select(ShopifyWebhookEvent.id).where(",
)  # behaviour is equivalent while the unique-constraint path works (defence in depth)
add(
    "dedup-02-race-path",
    SERVICE + "webhooks.py",
    "    except IntegrityError:\n        db.rollback()  # two deliveries raced",
    "    except ZeroDivisionError:\n        db.rollback()  # two deliveries raced",
    [*PG_RACE],
)
add(
    "dedup-03-both",
    SERVICE + "webhooks.py",
    "    if db.scalar(\n        select(ShopifyWebhookEvent.id).where(",
    "    if False and db.scalar(\n        select(ShopifyWebhookEvent.id).where(",
    UNIT,
)
add(
    "dedup-04-claim-not-atomic",
    SERVICE + "webhooks.py",
    "                ShopifyWebhookEvent.id == event_id,\n                ShopifyWebhookEvent.status == WebhookEventStatus.PENDING,\n            )",
    "                ShopifyWebhookEvent.id == event_id,\n            )",
    [*PG_RACE],
)
add(
    "dedup-05-key-ignores-shop",
    "app/db/models/shopify.py",
    '        UniqueConstraint(\n            "shop_domain", "webhook_id", name="uq_shopify_webhook_events_shop_webhook"\n        ),',
    "",
    MIG,
)
add(
    "dedup-06-id-scope",
    SERVICE + "webhooks.py",
    "            ShopifyWebhookEvent.shop_domain == shop, ShopifyWebhookEvent.webhook_id == webhook_id",
    "            ShopifyWebhookEvent.webhook_id == webhook_id",
)

# ---- sync authorization ---------------------------------------------------------------------------------------
add(
    "authz-01-sync-any-member",
    "app/api/integrations/shopify.py",
    "    ctx: ManagerOrg, db: DbSession, settings: AppSettings, queue: Queue, rt: Runtime",
    "    ctx: CurrentOrg, db: DbSession, settings: AppSettings, queue: Queue, rt: Runtime",
)
add(
    "authz-02-disconnect-any-member",
    "app/api/integrations/shopify.py",
    "    ctx: ManagerOrg, db: DbSession, settings: AppSettings, rt: Runtime, factory: Factory",
    "    ctx: CurrentOrg, db: DbSession, settings: AppSettings, rt: Runtime, factory: Factory",
)
add(
    "authz-03-connect-any-member",
    "app/api/integrations/shopify.py",
    "    body: ShopifyConnectIn, ctx: ManagerOrg, db: DbSession, settings: AppSettings, rt: Runtime",
    "    body: ShopifyConnectIn, ctx: CurrentOrg, db: DbSession, settings: AppSettings, rt: Runtime",
)
add(
    "authz-04-callback-any-member",
    "app/api/integrations/shopify.py",
    "    request: Request, ctx: ManagerOrg, db: DbSession, queue: Queue, rt: Runtime",
    "    request: Request, ctx: CurrentOrg, db: DbSession, queue: Queue, rt: Runtime",
)
add(
    "authz-05-job-runs-for-disconnected",
    SERVICE + "sync.py",
    '        if connection is None or connection.status != ShopifyConnectionStatus.SYNCING:\n            return "skipped"',
    '        if connection is None:\n            return "skipped"',
)
add(
    "authz-06-sync-claim-status",
    SERVICE + "service.py",
    "            ShopifyConnection.organization_id == org_id,\n            ShopifyConnection.status == ShopifyConnectionStatus.CONNECTED,",
    "            ShopifyConnection.organization_id == org_id,",
    [*PG, "-k", "racing"],
)
add(
    "authz-07-sync-rate-limit",
    SERVICE + "service.py",
    "        if wait > 0:\n            raise SyncTooSoon(int(wait) + 1)",
    "        if False:\n            raise SyncTooSoon(int(wait) + 1)",
)
add(
    "authz-08-reauth-gate",
    SERVICE + "service.py",
    "    if connection.status == ShopifyConnectionStatus.ERROR:",
    "    if False:",
)

# ---- embedding model separation ---------------------------------------------------------------------------------
add(
    "embed-01-search-model-filter",
    "app/services/rag/search.py",
    "            KnowledgeChunk.embedding_model == embedding_model,\n        )",
    "        )",
    EMB,
)
add(
    "embed-02-stale-diagnostic",
    "app/services/rag/search.py",
    "        if stale:",
    "        if False:",
    EMB,
)


add(
    "dedup-07-no-protection-at-all",
    SERVICE + "webhooks.py",
    [
        "    if db.scalar(\n        select(ShopifyWebhookEvent.id).where(",
        "    except IntegrityError:\n        db.rollback()  # two deliveries raced",
    ],
    [
        "    if False and db.scalar(\n        select(ShopifyWebhookEvent.id).where(",
        "    except ZeroDivisionError:\n        db.rollback()  # two deliveries raced",
    ],
    ["tests/shopify/test_webhooks.py"],
)


add(
    "log-01-filter-does-nothing",
    "app/core/logging.py",
    "            record.args = (*args[:2], redact_query(args[2]), *args[3:])",
    "            pass",
    ["tests/test_logging.py"],
)
add(
    "log-02-nothing-is-sensitive",
    "app/core/logging.py",
    '        "code",\n        "state",\n        "hmac",',
    '        "zz_code",\n        "zz_state",\n        "zz_hmac",',
    ["tests/test_logging.py"],
)
add(
    "log-03-filter-never-installed",
    "app/core/logging.py",
    "        access_logger.addFilter(RedactSensitiveQuery())",
    "        pass",
    ["tests/test_logging.py"],
)
