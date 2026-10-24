"""Mutants for the controls added in Phase 4.1: the production start-up gate, request limits and ids,
health and the worker, log redaction, secret masking, tenant/role enforcement, the Shopify error
and version handling, webhook self-healing, the disconnect race, the Anthropic adapter and the shared
HTTP client, the key-rotation tool and the database engine.
"""

M: list = []


def add(mid, file, old, new, args):
    M.append((mid, file, old, new, args))


def pg(path: str, keyword: str) -> list[str]:
    return ["-m", "integration", path, "-k", keyword]


STARTUP = ["tests/test_startup_checks.py"]
HARD = ["tests/test_http_hardening.py"]
HEALTH = ["tests/test_health_readiness.py"]
WORKER = [
    "tests/test_worker_process.py",
    "tests/test_health_readiness.py",
    "tests/test_startup_checks.py",
]
LOGR = ["tests/test_log_redaction.py", "tests/test_logging.py"]
TENANT = ["tests/test_tenant_api_matrix.py", "tests/test_tenancy.py"]
AI = ["tests/ai", "tests/tools"]
SHOP = ["tests/shopify"]
CHECKS = "app/core/startup_checks.py"
SERVICE = "app/services/integrations/shopify/"

# ---- production start-up gate -----------------------------------------------------------------------
add(
    "sg-01-weak-jwt-secret-allowed",
    CHECKS,
    "    reason = _weak_secret(settings.jwt_secret_key.get_secret_value())\n    if reason:",
    "    reason = None\n    if reason:",
    STARTUP,
)
add(
    "sg-02-placeholder-secret-allowed",
    CHECKS,
    "    if any(word in lowered for word in _PLACEHOLDER_WORDS):",
    "    if False:",
    STARTUP,
)
add(
    "sg-03-wildcard-cors-allowed",
    CHECKS,
    '        if origin == "*" or "*" in origin:',
    "        if False:",
    STARTUP,
)
add(
    "sg-04-plain-http-cors-allowed",
    CHECKS,
    '        elif parts.scheme != "https" or not parts.hostname:',
    "        elif not parts.hostname:",
    STARTUP,
)
add(
    "sg-05-localhost-cors-allowed",
    CHECKS,
    "        elif parts.hostname in _LOCAL_HOSTS:",
    "        elif False:",
    STARTUP,
)
add(
    "sg-06-development-db-password-allowed",
    CHECKS,
    "    elif url.password.lower() in _WEAK_PASSWORDS:",
    "    elif False:",
    STARTUP,
)
add(
    "sg-07-non-postgres-allowed",
    CHECKS,
    '    if url.get_backend_name() != "postgresql":',
    "    if False:",
    STARTUP,
)
add(
    "sg-08-redis-default-allowed",
    CHECKS,
    '    if "redis_url" not in settings.model_fields_set:',
    "    if False:",
    STARTUP,
)
add(
    "sg-09-forwarded-peers-not-required",
    CHECKS,
    "    if not forwarded:",
    "    if False:",
    STARTUP,
)
add(
    "sg-10-missing-encryption-key-allowed",
    CHECKS,
    "        SecretManager.from_settings(settings)\n    except SecretError as exc:",
    "        pass\n    except SecretError as exc:",
    STARTUP,
)
add(
    "sg-11-webhook-decision-not-required",
    CHECKS,
    "    if not base and not settings.shopify_webhooks_disabled:",
    "    if False:",
    STARTUP,
)
add(
    "sg-12-http-webhook-url-allowed",
    CHECKS,
    '    elif base and not base.lower().startswith("https://"):',
    "    elif False:",
    STARTUP,
)
add(
    "sg-13-debug-logging-allowed",
    CHECKS,
    '    if settings.log_level == "DEBUG":',
    "    if False:",
    STARTUP,
)
add(
    "sg-14-unsafe-config-still-starts",
    CHECKS,
    "        raise UnsafeConfiguration(findings.problems)",
    "        pass",
    STARTUP,
)
add(
    "sg-15-production-not-recognised",
    "app/core/config.py",
    '    def is_production(self) -> bool:\n        return self.environment == "production"',
    "    def is_production(self) -> bool:\n        return False",
    STARTUP,
)
add(
    "sg-16-docs-served-in-production",
    "app/core/config.py",
    "else not self.is_production",
    "else True",
    STARTUP,
)
add(
    "sg-17-worker-skips-the-gate",
    "app/workers/main.py",
    "    enforce_startup(settings)  # production: refuse to start with an unsafe configuration",
    "    pass",
    STARTUP,
)
add(
    "sg-18-api-skips-the-gate",
    "app/main.py",
    "    enforce_startup(settings)  # production: refuse to start with an unsafe configuration",
    "    pass",
    STARTUP,
)

# ---- request limits, request ids, failure responses -------------------------------------------------------
add(
    "hd-01-declared-length-not-checked",
    "app/core/body_limit.py",
    "        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:",
    "        if False:",
    HARD,
)
add(
    "hd-02-streamed-body-not-counted",
    "app/core/body_limit.py",
    "            if len(body) > self.max_bytes:",
    "            if False:",
    HARD,
)
add(
    "hd-03-every-path-exempt-from-the-cap",
    "app/core/body_limit.py",
    '            or scope["path"].startswith(self.exempt)',
    "            or True",
    HARD,
)
add(
    "hd-04-webhook-path-not-exempt",
    "app/core/body_limit.py",
    'DEFAULT_EXEMPT_PREFIXES = ("/api/public/", "/api/webhooks/")',
    'DEFAULT_EXEMPT_PREFIXES = ("/api/public/",)',
    HARD,
)
add(
    "hd-05-hostile-request-id-echoed",
    "app/core/request_context.py",
    '_VALID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")',
    '_VALID = re.compile(r"^.*$")',
    HARD,
)
add(
    "hd-06-request-id-missing-from-responses",
    "app/core/request_context.py",
    '                headers.append((b"x-request-id", request_id.encode()))',
    "                pass",
    HARD,
)
add(
    "hd-07-request-id-not-in-log-lines",
    "app/core/request_context.py",
    '        record.request_suffix = f" request_id={request_id}" if request_id else ""',
    '        record.request_suffix = ""',
    HARD,
)
add(
    "hd-08-500-reveals-exception-text",
    "app/main.py",
    '            {"detail": "Internal server error", "request_id": request_id},',
    '            {"detail": f"Internal server error: {exc}", "request_id": request_id},',
    HARD,
)
add(
    "hd-09-database-outage-is-a-500",
    "app/main.py",
    "    for exc_type in (OperationalError, InterfaceError, PoolTimeoutError, RedisError):",
    "    for exc_type in (InterfaceError, PoolTimeoutError, RedisError):",
    HARD,
)
add(
    "hd-10-outage-without-retry-after",
    "app/main.py",
    '            headers={"Retry-After": "5"},',
    "            headers=None,",
    HARD,
)
add(
    "hd-11-cors-allows-any-header",
    "app/main.py",
    "        allow_headers=DASHBOARD_HEADERS,",
    '        allow_headers=["*"],',
    HARD,
)
add(
    "hd-12-outage-reveals-cause",
    "app/main.py",
    '                "detail": "Service temporarily unavailable. Please retry.",',
    '                "detail": f"Service temporarily unavailable: {exc}",',
    HARD,
)

# ---- health and the worker --------------------------------------------------------------------------------
add(
    "hl-01-readiness-ignores-redis",
    "app/api/health/router.py",
    '        "redis": "ok" if _redis_ok(queue) else "unavailable",',
    '        "redis": "ok",',
    HEALTH,
)
add(
    "hl-02-readiness-ignores-the-database",
    "app/api/health/router.py",
    '        "database": "ok" if _database_ok(db) else "unavailable",',
    '        "database": "ok",',
    HEALTH,
)
add(
    "hl-03-unready-still-answers-200",
    "app/api/health/router.py",
    "    if not healthy:\n        response.status_code = 503",
    "    if False:\n        response.status_code = 503",
    HEALTH,
)
add(
    "hl-04-worker-probe-always-ok",
    "app/api/health/router.py",
    '    state = "ok" if workers > 0 else ("unknown" if workers < 0 else "none")',
    '    state = "ok"',
    HEALTH,
)
add(
    "wk-01-stopped-worker-keeps-its-heartbeat",
    "app/workers/heartbeat.py",
    "            self._path.unlink(missing_ok=True)  # a stopped worker must not look alive",
    "            pass",
    HEALTH,
)
add(
    "wk-02-a-crashing-job-kills-the-worker",
    "app/workers/main.py",
    "    except Exception:\n        # One bad job must never take the worker down",
    "    except KeyboardInterrupt:\n        # One bad job must never take the worker down",
    WORKER,
)
add(
    "wk-03-a-redis-outage-kills-the-worker",
    "app/workers/main.py",
    '    except redis.RedisError:\n        logger.warning("redis unavailable, retrying")',
    '    except ZeroDivisionError:\n        logger.warning("redis unavailable, retrying")',
    WORKER,
)
add(
    "wk-04-no-pause-after-a-crash",
    "app/workers/main.py",
    "        stop.wait(CRASH_BACKOFF_SECONDS)",
    "        pass",
    WORKER,
)
add(
    "wk-05-job-id-leaks-into-the-next-job",
    "app/workers/main.py",
    "    finally:\n        set_request_id(None)\n    return True",
    "    finally:\n        pass\n    return True",
    WORKER,
)

# ---- logs and settings never reveal secrets ---------------------------------------------------------------
add(
    "lr-01-formatter-redacts-nothing",
    "app/core/logging.py",
    "        return redact_secrets(super().format(record))",
    "        return super().format(record)",
    LOGR,
)
add(
    "lr-02-database-echoed-values-kept",
    "app/core/logging.py",
    r'        r"Key (\g<cols>)=([redacted])",',
    r'        r"Key (\g<cols>)=(\g<value>)",',
    LOGR,
)
add(
    "lr-03-shopify-tokens-not-recognised",
    "app/core/logging.py",
    r'(re.compile(r"\bshp(?:at|ca|ss|pa|rt)_',
    r'(re.compile(r"\bzzz(?:at|ca|ss|pa|rt)_',
    LOGR,
)
add(
    "lr-04-bearer-credentials-not-recognised",
    "app/core/logging.py",
    r"(?i)\b(bearer|basic)\s+",
    r"(?i)\b(zzzzzr|zzzzc)\s+",
    LOGR,
)
add(
    "lr-05-http-client-urls-logged",
    "app/core/logging.py",
    "        logging.getLogger(name).setLevel(logging.WARNING)",
    "        pass",
    LOGR,
)
add(
    "lr-06-worker-logs-unredacted",
    "app/workers/main.py",
    '        SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s%(request_suffix)s")',
    '        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s%(request_suffix)s")',
    LOGR,
)
add(
    "lr-07-api-logs-unredacted",
    "app/core/logging.py",
    '        SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s%(request_suffix)s")',
    '        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s%(request_suffix)s")',
    LOGR,
)
add(
    "sc-01-url-credentials-shown-in-settings-repr",
    "app/core/config.py",
    '                value = _URL_CREDENTIALS.sub(r"\\g<head>:***@", value)',
    "                pass",
    ["tests/test_settings_secrecy.py"],
)
add(
    "db-01-sql-errors-show-bound-values",
    "app/db/database.py",
    '    kwargs: dict[str, Any] = {"pool_pre_ping": True, "hide_parameters": True}',
    '    kwargs: dict[str, Any] = {"pool_pre_ping": True, "hide_parameters": False}',
    ["tests/test_db_engine.py"],
)
add(
    "db-02-no-statement-timeout",
    "app/db/database.py",
    '            "statement_timeout": settings.db_statement_timeout_seconds * 1000,',
    '            "statement_timeout": 0,',
    pg("tests/integration/test_platform_hardening.py", "server_side_limits or runaway"),
)
add(
    "db-03-no-lock-timeout",
    "app/db/database.py",
    '            "lock_timeout": settings.db_lock_timeout_seconds * 1000,',
    '            "lock_timeout": 0,',
    pg("tests/integration/test_platform_hardening.py", "server_side_limits or contended"),
)
add(
    "db-04-no-idle-transaction-timeout",
    "app/db/database.py",
    "            * 1000,\n        }",
    "            * 0,\n        }",
    pg("tests/integration/test_platform_hardening.py", "server_side_limits or vanishes"),
)

# ---- authentication, roles, tenants -------------------------------------------------------------------------
add(
    "tn-01-header-org-role-not-checked",
    "app/api/deps.py",
    "    def dependency(ctx: CurrentOrg) -> OrgContext:\n        if ctx.role not in allowed:",
    "    def dependency(ctx: CurrentOrg) -> OrgContext:\n        if False:",
    TENANT,
)
add(
    "tn-02-path-org-role-not-checked",
    "app/api/deps.py",
    "    def dependency(ctx: PathOrg) -> OrgContext:\n        if ctx.role not in allowed:",
    "    def dependency(ctx: PathOrg) -> OrgContext:\n        if False:",
    TENANT,
)
add(
    "tn-03-any-org-id-accepted-without-membership",
    "app/services/organizations/service.py",
    "        stmt = stmt.where(OrganizationMember.organization_id == organization_id)",
    "        pass",
    TENANT,
)
add(
    "tn-04-foreign-org-is-403-not-404",
    "app/api/deps.py",
    '    ctx = resolve_org_context(db, user, organization_id)\n    if ctx is None:\n        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")',
    '    ctx = resolve_org_context(db, user, organization_id)\n    if ctx is None:\n        raise HTTPException(status.HTTP_403_FORBIDDEN, "Organization not found")',
    TENANT,
)
add(
    "tn-05-no-credentials-is-not-a-401",
    "app/api/deps.py",
    "    if credentials is None:\n        raise _unauthorized()",
    "    if False:\n        raise _unauthorized()",
    TENANT,
)

# ---- Shopify error handling, versions, self-healing, disconnect --------------------------------------------------
add(
    "sh-01-403-is-not-access-denied",
    SERVICE + "client.py",
    "            if status == 403:",
    "            if status == 999:",
    SHOP,
)
add(
    "sh-02-shop-unavailable-statuses-forgotten",
    SERVICE + "client.py",
    "            if status in (402, 404, 423):  # frozen (unpaid), gone, or locked",
    "            if status in (404,):  # frozen (unpaid), gone, or locked",
    SHOP,
)
add(
    "sh-03-fall-forward-never-reported",
    SERVICE + "client.py",
    "            self._check_served_version(response)",
    "            pass",
    SHOP,
)
add(
    "sh-04-missing-subscriptions-not-recreated",
    SERVICE + "subscriptions.py",
    "        register_webhooks(rt, session_factory, connection_id)  # creates only what is missing",
    "        pass",
    SHOP,
)
add(
    "sh-05-no-catch-up-sync-after-recreating",
    SERVICE + "subscriptions.py",
    "        started = believed_live and queue_sync(session_factory, queue, connection_id)",
    "        started = False",
    SHOP,
)
add(
    "sh-06-sync-page-not-protected-from-a-disconnect",
    SERVICE + "sync.py",
    "                .with_for_update()\n            )\n            if status != SyncStatus.RUNNING:",
    "            )\n            if status != SyncStatus.RUNNING:",
    pg("tests/integration/test_shopify_postgres.py", "disconnect_during_a_page_write"),
)
add(
    "sh-07-refresh-lock-timeout-is-a-database-crash",
    SERVICE + "auth.py",
    '                if getattr(exc.orig, "sqlstate", None) != LOCK_NOT_AVAILABLE:',
    "                if True:",
    pg("tests/integration/test_shopify_postgres.py", "cannot_get_the_connection_lock"),
)
add(
    "sh-08-purge-safety-net-ignores-orders",
    SERVICE + "lifecycle.py",
    "                for model in (Product, Customer, Order)",
    "                for model in (Product, Customer)",
    ["tests/shopify/test_disconnect_retention.py"],
)
add(
    "sh-09-purge-safety-net-visits-empty-stores",
    SERVICE + "lifecycle.py",
    "                    ShopifyConnection.status == ShopifyConnectionStatus.DISCONNECTED,\n                    has_rows,\n",
    "                    ShopifyConnection.status == ShopifyConnectionStatus.DISCONNECTED,\n",
    ["tests/shopify/test_disconnect_retention.py"],
)
add(
    "sh-10-purge-safety-net-purges-connected-stores",
    SERVICE + "lifecycle.py",
    "                    ShopifyConnection.status == ShopifyConnectionStatus.DISCONNECTED,\n                    has_rows,\n",
    "                    ShopifyConnection.status != ShopifyConnectionStatus.DISCONNECTED,\n                    has_rows,\n",
    ["tests/shopify/test_disconnect_retention.py"],
)

# ---- AI adapters and the shared HTTP client -----------------------------------------------------------------------
ANTHROPIC = "app/services/ai/providers/anthropic_messages.py"
add(
    "ai-01-key-sent-as-bearer-token",
    ANTHROPIC,
    '            auth_header="x-api-key",',
    "            auth_header=None,",
    AI,
)
add(
    "ai-02-no-api-version-header",
    ANTHROPIC,
    '            extra_headers={"anthropic-version": ANTHROPIC_VERSION},',
    "            extra_headers=None,",
    AI,
)
add(
    "ai-03-system-prompt-sent-as-a-turn",
    ANTHROPIC,
    '            {"role": m.role, "content": m.content} for m in request.messages if m.role != "system"',
    '            {"role": m.role, "content": m.content} for m in request.messages if True',
    AI,
)
add(
    "ai-04-conversation-may-end-on-assistant",
    ANTHROPIC,
    '        if not turns or turns[-1]["role"] != "user":',
    "        if not turns:",
    AI,
)
add(
    "ai-05-temperature-not-clamped",
    ANTHROPIC,
    '            payload["temperature"] = min(max(float(temperature), 0.0), 1.0)  # Anthropic: 0 to 1',
    '            payload["temperature"] = float(temperature)',
    AI,
)
add(
    "ai-06-refusal-not-content-filtered",
    ANTHROPIC,
    '        if stop == "refusal":',
    "        if False:",
    AI,
)
add(
    "ai-07-payment-required-is-not-an-auth-problem",
    "app/services/ai/errors.py",
    "    if status in (401, 402, 403):",
    "    if status in (401, 403):",
    AI,
)
add(
    "ai-08-response-size-uncapped",
    "app/services/ai/providers/_http.py",
    "                if size > limit:",
    "                if False:",
    AI,
)
add(
    "ai-09-slow-bodies-not-cut-off",
    "app/services/ai/providers/_http.py",
    "                if time.monotonic() > deadline:",
    "                if False:",
    AI,
)

# ---- key rotation tool ---------------------------------------------------------------------------------------------------
TOOL = "tools/reencrypt_secrets.py"
ROTATION = ["tests/shopify/test_key_rotation.py"]
add(
    "rt-01-refresh-tokens-never-rotated",
    TOOL,
    '    ("access_token", "access_token_encrypted"),\n    ("refresh_token", "refresh_token_encrypted"),\n',
    '    ("access_token", "access_token_encrypted"),\n',
    ROTATION,
)
add(
    "rt-02-unreadable-secrets-not-counted",
    TOOL,
    "                    report.unreadable += 1",
    "                    report.unreadable += 0",
    ROTATION,
)
add(
    "rt-03-nothing-ever-needs-rotation",
    TOOL,
    "                    if not secrets.needs_rotation(stored):",
    "                    if True:",
    ROTATION,
)
