"""The production start-up gate: refuse to run with an unsafe configuration.

``Settings`` validates each value on its own (types, ranges, "no fake AI in production"). This
module looks at the deployment as a whole, once, when the API or the worker starts. In production
it turns every unsafe finding into a refusal to start, listing ALL of them so an operator fixes the
configuration in one pass instead of one restart per problem. In staging the same findings are
logged as warnings, so staging shows what production would reject.

Nothing here prints a secret value: findings name the setting and the rule, never the value.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.core.secrets import SecretError, SecretManager
from app.services.ai.errors import AIError
from app.services.ai.registry import resolve_embedding_config, resolve_llm_config

logger = logging.getLogger(__name__)

# Passwords and secrets that appear in tutorials, compose files and ".env.example" files.
_WEAK_PASSWORDS = frozenset(
    {"bizflow", "postgres", "password", "passw0rd", "changeme", "admin", "root", "secret", "123456"}
)
# Words that mean "someone forgot to replace the placeholder".
_PLACEHOLDER_WORDS = (
    "changeme",
    "change-me",
    "change_me",
    "placeholder",
    "dummy",
    "example",
    "your-",
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})
MIN_SECRET_DISTINCT_CHARACTERS = 12


class UnsafeConfiguration(RuntimeError):
    """Production refused to start. ``problems`` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(
            "Refusing to start with an unsafe production configuration:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


@dataclass(frozen=True)
class Findings:
    problems: list[str]
    warnings: list[str]


def _weak_secret(value: str) -> str | None:
    lowered = value.lower()
    if len(set(value)) < MIN_SECRET_DISTINCT_CHARACTERS:
        return f"uses fewer than {MIN_SECRET_DISTINCT_CHARACTERS} distinct characters"
    if any(word in lowered for word in _PLACEHOLDER_WORDS):
        return "looks like a placeholder"
    return None


def _check_database(settings: Settings, problems: list[str], warnings: list[str]) -> None:
    try:
        url = make_url(settings.database_url)
    except Exception:  # noqa: BLE001 - any parse failure is the same finding
        problems.append("DATABASE_URL is not a valid SQLAlchemy URL")
        return
    if url.get_backend_name() != "postgresql":
        problems.append("DATABASE_URL must be PostgreSQL in production (pgvector is required)")
        return
    if not url.password:
        problems.append("DATABASE_URL has no password")
    elif url.password.lower() in _WEAK_PASSWORDS:
        problems.append("DATABASE_URL uses a well-known development password")
    if (url.host or "") not in _LOCAL_HOSTS and "sslmode" not in url.query:
        warnings.append(
            "DATABASE_URL has no sslmode: connections to a remote database should use TLS"
        )


def _check_cors(settings: Settings, problems: list[str]) -> None:
    origins = settings.cors_origin_list
    if not origins:
        problems.append("CORS_ORIGINS is empty")
    for origin in origins:
        parts = urlsplit(origin)
        if origin == "*" or "*" in origin:
            problems.append("CORS_ORIGINS must list exact origins, never a wildcard")
        elif parts.scheme != "https" or not parts.hostname:
            problems.append(f"CORS_ORIGINS entry {origin!r} must be an https:// origin")
        elif parts.hostname in _LOCAL_HOSTS:
            problems.append("CORS_ORIGINS must not allow localhost in production")
        elif parts.path not in ("", "/") or parts.query or parts.fragment:
            problems.append(f"CORS_ORIGINS entry {origin!r} must be an origin, without a path")


def _check_redis(settings: Settings, problems: list[str], warnings: list[str]) -> None:
    if "redis_url" not in settings.model_fields_set:
        problems.append(
            "REDIS_URL must be set explicitly (the localhost default is for development)"
        )
        return
    parts = urlsplit(settings.redis_url)
    if parts.scheme not in ("redis", "rediss", "unix"):
        problems.append("REDIS_URL must start with redis://, rediss:// or unix://")
    elif (parts.hostname or "") not in _LOCAL_HOSTS and parts.scheme == "redis":
        if not parts.password:
            warnings.append("REDIS_URL points at a remote Redis without a password")
        warnings.append(
            "REDIS_URL is not TLS (rediss://): job data would cross the network in clear"
        )


def _check_shopify(settings: Settings, problems: list[str]) -> None:
    has_id = bool(settings.shopify_client_id.strip())
    has_secret = bool(
        settings.shopify_client_secret and settings.shopify_client_secret.get_secret_value().strip()
    )
    if has_id != has_secret:
        problems.append("SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET must be set together")
    if not settings.shopify_configured:
        return
    try:
        SecretManager.from_settings(settings)
    except SecretError as exc:
        problems.append(
            f"Shopify is configured but SECRET_ENCRYPTION_KEY is unusable (code={exc.code}); "
            "tokens could not be stored safely"
        )
    base = settings.shopify_webhook_base_url.strip()
    if not base and not settings.shopify_webhooks_disabled:
        problems.append(
            "set SHOPIFY_WEBHOOK_BASE_URL (the public https URL of this API) or explicitly "
            "SHOPIFY_WEBHOOKS_DISABLED=true: live updates must be a decision, not an accident"
        )
    elif base and not base.lower().startswith("https://"):
        problems.append("SHOPIFY_WEBHOOK_BASE_URL must be an https:// URL (Shopify requires it)")


def _check_whatsapp(settings: Settings, problems: list[str]) -> None:
    has_id = bool(settings.whatsapp_app_id.strip())
    has_secret = bool(
        settings.whatsapp_app_secret and settings.whatsapp_app_secret.get_secret_value().strip()
    )
    if has_id != has_secret:
        problems.append("WHATSAPP_APP_ID and WHATSAPP_APP_SECRET must be set together")
    if not settings.whatsapp_configured:
        return
    try:
        SecretManager.from_settings(settings)
    except SecretError as exc:
        problems.append(
            f"WhatsApp is configured but SECRET_ENCRYPTION_KEY is unusable (code={exc.code}); "
            "tokens could not be stored safely"
        )
    if not settings.whatsapp_webhooks_disabled:
        verify_token = (
            settings.whatsapp_webhook_verify_token.get_secret_value().strip()
            if settings.whatsapp_webhook_verify_token
            else ""
        )
        if not verify_token:
            problems.append(
                "set WHATSAPP_WEBHOOK_VERIFY_TOKEN (configured in the Meta App's webhook "
                "subscription) or explicitly WHATSAPP_WEBHOOKS_DISABLED=true: receiving inbound "
                "messages and delivery status must be a decision, not an accident"
            )


def _check_email(settings: Settings, problems: list[str], warnings: list[str]) -> None:
    if not settings.email_configured:
        return
    if not settings.email_webhooks_disabled:
        secret = (
            settings.email_webhook_secret.get_secret_value().strip()
            if settings.email_webhook_secret
            else ""
        )
        if not secret:
            problems.append(
                "set EMAIL_WEBHOOK_SECRET (the provider's signing secret) or explicitly "
                "EMAIL_WEBHOOKS_DISABLED=true: receiving inbound email and delivery status must "
                "be a decision, not an accident"
            )
        if settings.email_inbound_domain.endswith(".invalid"):
            problems.append(
                "EMAIL_INBOUND_DOMAIN is still the placeholder (.invalid) domain; set it to a "
                "real domain verified with the email provider before inbound email can route "
                "anywhere"
            )


def _check_ai(settings: Settings, problems: list[str]) -> None:
    for label, resolve in (("LLM", resolve_llm_config), ("EMBEDDING", resolve_embedding_config)):
        try:
            resolve(settings)
        except AIError as exc:  # the message names the setting, never a key
            problems.append(f"{label} provider is not usable: {exc}")


def _check_platform_admin(settings: Settings, problems: list[str]) -> None:
    """PLATFORM_ADMIN_* only configures the seed command, not request handling -- but a weak or
    placeholder value there would still mean a guessable founder/platform login in production."""
    if settings.platform_admin_password is None:
        return
    reason = _weak_secret(settings.platform_admin_password.get_secret_value())
    if reason:
        problems.append(f"PLATFORM_ADMIN_PASSWORD {reason}")


def check_configuration(settings: Settings, environ: Mapping[str, str] | None = None) -> Findings:
    """Everything wrong (``problems``) or doubtful (``warnings``) about this deployment."""
    env = os.environ if environ is None else environ
    problems: list[str] = []
    warnings: list[str] = []

    reason = _weak_secret(settings.jwt_secret_key.get_secret_value())
    if reason:
        problems.append(
            f'JWT_SECRET_KEY {reason}; generate one with `python -c "import secrets; '
            'print(secrets.token_urlsafe(48))"`'
        )
    _check_database(settings, problems, warnings)
    _check_cors(settings, problems)
    _check_redis(settings, problems, warnings)

    forwarded = (settings.forwarded_allow_ips or env.get("FORWARDED_ALLOW_IPS", "")).strip()
    if not forwarded:
        problems.append(
            "FORWARDED_ALLOW_IPS must be set explicitly: the peers allowed to set X-Forwarded-For "
            "(your proxy's address). Without it, rate limiting keys on the proxy, not on clients"
        )
    elif forwarded == "*":
        warnings.append(
            "FORWARDED_ALLOW_IPS=* trusts every peer; safe only if the API is "
            "unreachable except through your proxy"
        )

    _check_shopify(settings, problems)
    _check_whatsapp(settings, problems)
    _check_email(settings, problems, warnings)
    _check_ai(settings, problems)
    _check_platform_admin(settings, problems)

    if settings.log_level == "DEBUG":
        problems.append("LOG_LEVEL=DEBUG is not allowed in production (use INFO)")
    if settings.docs_enabled:
        warnings.append("API_DOCS_ENABLED exposes /docs and /openapi.json publicly")
    if settings.rate_limit_backend == "memory":
        warnings.append(
            "RATE_LIMIT_BACKEND=memory: limits are per process, so with several API workers or "
            "replicas they are multiplied; use RATE_LIMIT_BACKEND=redis"
        )
    return Findings(problems, warnings)


def enforce_startup(settings: Settings, environ: Mapping[str, str] | None = None) -> Findings:
    """Called by the API and the worker at start. Production: unsafe means refuse to start.
    Elsewhere the same findings are logged, so staging shows what production would reject."""
    if not settings.is_production:
        if settings.environment == "staging":
            findings = check_configuration(settings, environ)
            for item in (*findings.problems, *findings.warnings):
                logger.warning("staging configuration would be refused in production: %s", item)
            return findings
        return Findings([], [])
    findings = check_configuration(settings, environ)
    for item in findings.warnings:
        logger.warning("production configuration warning: %s", item)
    if findings.problems:
        for item in findings.problems:
            logger.critical("unsafe production configuration: %s", item)
        raise UnsafeConfiguration(findings.problems)
    return findings
