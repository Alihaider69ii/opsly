import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.ai.catalog import non_production_provider_ids
from app.services.integrations.shopify.scopes import DEFAULT_SCOPES
from app.services.integrations.shopify.versioning import CURRENT_API_VERSION, is_quarterly_version

MIN_JWT_SECRET_LENGTH = 32
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
# The deployment environments the application knows. Anything else is refused: a typo such as
# ENVIRONMENT=prod would otherwise silently skip every production safeguard.
ENVIRONMENTS = ("development", "test", "staging", "production")
# ``scheme://user:password@host`` -> ``scheme://user:***@host`` (for repr only)
_URL_CREDENTIALS = re.compile(r"(?P<head>[a-z][a-z0-9+.-]*://[^:/@\s]*):[^@\s]*@", re.I)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,  # startup errors must not echo DATABASE_URL or the JWT secret
    )

    app_name: str = "BizFlow AI"
    environment: str = "development"
    # Serve the interactive API docs (/docs, /redoc, /openapi.json). None = on except in production.
    api_docs_enabled: bool | None = None
    # Level for the application's own loggers (``app.*``); see core/logging.configure_logging.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@localhost:5432/bizflow
    database_url: str

    # Comma-separated list of allowed browser origins.
    cors_origins: str = "http://localhost:3000"

    # Signing key for access tokens. Required; the app refuses to start without a strong one.
    # Generate: python -c "import secrets; print(secrets.token_urlsafe(48))"
    jwt_secret_key: SecretStr
    access_token_expire_minutes: int = 480

    # Per-client-IP attempts allowed per window on /api/auth/login and /api/auth/register.
    auth_rate_limit_attempts: int = 10
    auth_rate_limit_window_seconds: int = 60
    # Per-client-IP Shopify OAuth connect/callback attempts; keep the production default bounded.
    shopify_oauth_rate_limit: int = Field(default=20, ge=1, le=1000)

    # Uvicorn reads this itself to decide which peers may set X-Forwarded-For; the application reads
    # it too, so production can refuse to start without an explicit decision (see startup_checks).
    forwarded_allow_ips: str = ""

    # Largest request body accepted by any endpoint except the public chat API (its own, much
    # smaller cap) and the Shopify webhook (its own, see SHOPIFY_WEBHOOK_MAX_BODY_BYTES).
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024)

    # --- Database ----------------------------------------------------------------------------
    # PostgreSQL only. A stuck query, lock or open transaction must never hold a connection for
    # ever.
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    db_statement_timeout_seconds: int = Field(default=120, ge=1)
    db_lock_timeout_seconds: int = Field(default=30, ge=1)
    db_idle_in_transaction_timeout_seconds: int = Field(default=300, ge=5)

    # --- Job queue ---------------------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    # Prefix of the Redis lists that carry jobs. Give each environment sharing one Redis its own.
    redis_queue_key: str = "bizflow:queue:crawl"

    # --- AI providers ------------------------------------------------------------------------
    # The LLM and the embeddings are configured independently: answers may come from one vendor
    # and vectors from another. A provider id selects a row of the provider catalog
    # (app/services/ai/catalog.py): openai, openrouter, groq, together, deepseek, mistral, gemini,
    # custom_openai_compatible (any OpenAI-compatible endpoint; set *_BASE_URL) or fake (offline
    # development only, refused when ENVIRONMENT=production). Model names are configuration, never
    # code. A blank *_BASE_URL / *_MODEL means "the provider's default".
    #
    # Embeddings. EMBEDDING_DIMENSIONS must equal the vector column dimension
    # (app.db.models.knowledge.EMBEDDING_DIM).
    embedding_provider: str = "openai"
    embedding_model: str = ""
    embedding_dimensions: int = 1536
    embedding_batch_size: int = Field(default=64, ge=1, le=256)
    embedding_base_url: str = ""
    embedding_api_key: SecretStr | None = None
    embedding_timeout_seconds: float = Field(default=30.0, gt=0)
    # Legacy fallback: used as the API key of the "openai" provider when LLM_API_KEY /
    # EMBEDDING_API_KEY are unset, so deployments that only ever set OPENAI_API_KEY keep working.
    openai_api_key: SecretStr | None = None

    # LLM.
    llm_provider: str = "openai"
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key: SecretStr | None = None
    # Override the request field carrying the output limit ("max_tokens" or "max_completion_tokens")
    # for endpoints that differ from the provider's catalog default.
    llm_max_tokens_field: str = ""
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_max_output_tokens: int = Field(default=600, ge=16, le=8000)
    llm_temperature: float | None = Field(default=None, ge=0, le=2)  # None = provider default

    # --- RAG and chat ------------------------------------------------------------------------
    rag_top_k: int = Field(default=5, ge=1, le=20)
    # Minimum cosine similarity for a chunk to count as relevant. Model-dependent: tune it against
    # real questions (text-embedding-3 relevant matches usually score ~0.3-0.6, unrelated < 0.2).
    rag_similarity_threshold: float = Field(default=0.25, ge=0, le=1)
    rag_max_context_chars: int = Field(default=6000, ge=500, le=40_000)
    chat_history_limit: int = Field(default=10, ge=0, le=50)
    chat_max_message_chars: int = Field(default=2000, ge=1, le=10_000)
    chat_max_response_chars: int = Field(default=2000, ge=100, le=10_000)
    chat_max_messages_per_conversation: int = Field(default=100, ge=2)
    # Shown instead of calling the LLM when the knowledge base has nothing relevant.
    chat_fallback_message: str = (
        "I don't have enough information to answer that accurately. "
        "Could you share a little more about what you're looking for?"
    )

    # --- Public chat abuse protection --------------------------------------------------------
    # Messages allowed per client IP and public key per window.
    public_chat_rate_limit: int = Field(default=20, ge=1)
    public_chat_rate_window_seconds: int = Field(default=60, ge=1)
    # New conversations allowed per client IP and public key per hour.
    public_chat_conversation_limit: int = Field(default=10, ge=1)
    public_chat_max_body_bytes: int = Field(default=16_384, ge=1024)
    # "memory" is per process; "redis" shares counters across API workers (falls back to memory if
    # Redis is unreachable, so a Redis outage degrades protection instead of the service).
    rate_limit_backend: Literal["memory", "redis"] = "memory"

    # --- Secrets at rest ---------------------------------------------------------------------
    # AES-256-GCM key (32 random bytes, base64) for integration tokens; see app/core/secrets.py.
    # Required before any integration can be connected. Rotate by deploying the new key as current
    # and the old one in SECRET_ENCRYPTION_PREVIOUS_KEYS ("id:key,id:key"), which can still decrypt.
    secret_encryption_key: SecretStr | None = None
    secret_encryption_key_id: str = "1"
    secret_encryption_previous_keys: SecretStr | None = None

    # --- Shopify -----------------------------------------------------------------------------
    # The Shopify app's credentials (Dev Dashboard / Partner Dashboard). The client secret signs
    # OAuth callbacks and webhooks and is exchanged for tokens; it never leaves the server.
    shopify_client_id: str = ""
    shopify_client_secret: SecretStr | None = None
    # Where Shopify sends the merchant after approval. It must exactly match a redirect URL
    # registered for the app, and points at the Next.js frontend, which holds the user's session.
    shopify_redirect_uri: str = "http://localhost:3000/integrations/shopify/callback"
    shopify_api_version: str = CURRENT_API_VERSION
    shopify_scopes: str = ",".join(DEFAULT_SCOPES)
    # Public https base URL of this API, used to register webhooks ("" = do not register, which is
    # the right setting on localhost: Shopify cannot reach it). See docs/shopify.md.
    shopify_webhook_base_url: str = ""
    # Development only (refused in production): talk to a local fake Shopify instead of
    # https://<shop>.myshopify.com. Shop domains are still validated as *.myshopify.com.
    shopify_dev_base_url: str = ""
    shopify_oauth_state_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    shopify_request_timeout_seconds: float = Field(default=30.0, gt=0)
    shopify_max_retries: int = Field(default=5, ge=0, le=10)
    shopify_sync_min_interval_seconds: int = Field(default=60, ge=0)
    # One job runs for at most this long, then saves its place and re-queues itself, so a large
    # import cannot starve crawls and webhooks.
    shopify_sync_slice_seconds: float = Field(default=120.0, gt=1)
    shopify_sync_stale_after_seconds: int = Field(default=600, ge=60)
    shopify_webhook_max_body_bytes: int = Field(default=2_000_000, ge=1024)
    # Set when this deployment deliberately has no public webhook endpoint (data is then only as
    # fresh as the last sync). Production refuses to start with Shopify configured and neither
    # this nor SHOPIFY_WEBHOOK_BASE_URL set, so "no live updates" is a decision, never an accident.
    shopify_webhooks_disabled: bool = False
    shopify_webhook_retention_days: int = Field(default=30, ge=1)
    # How often the worker checks that each store still has BizFlow's webhook subscriptions
    # (Shopify deletes API-created ones after 8 consecutive failed deliveries) and repairs them.
    shopify_webhook_audit_interval_seconds: int = Field(default=3600, ge=60)

    # --- Chunking ----------------------------------------------------------------------------
    chunk_size: int = Field(default=1200, ge=200, le=4000)  # hard upper bound, in characters
    chunk_overlap: int = Field(default=150, ge=0)

    # --- Website crawler ---------------------------------------------------------------------
    crawl_max_pages: int = Field(default=50, ge=1, le=500)
    crawl_max_depth: int = Field(default=3, ge=0, le=10)
    crawl_max_response_bytes: int = Field(default=2_000_000, ge=10_000)
    crawl_max_document_chars: int = Field(default=100_000, ge=1_000)
    crawl_min_content_chars: int = Field(default=40, ge=0)
    crawl_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    crawl_read_timeout_seconds: float = Field(default=10.0, gt=0)
    # Wall-clock cap for one HTTP request (defeats servers that trickle bytes).
    crawl_request_timeout_seconds: float = Field(default=20.0, gt=0)
    # Wall-clock cap for one whole crawl; the crawl stops (not fails) when reached.
    crawl_total_timeout_seconds: float = Field(default=600.0, gt=0)
    crawl_max_redirects: int = Field(default=5, ge=0, le=10)
    crawl_delay_seconds: float = Field(default=0.5, ge=0)  # politeness delay between requests
    crawl_max_robots_delay_seconds: float = Field(default=10.0, ge=0)
    # Only these destination ports may be contacted (initial URL and every redirect).
    crawl_allowed_ports: str = "80,443"
    crawl_user_agent: str = "BizFlowBot/1.0"
    # Product token matched against robots.txt "User-agent" groups.
    crawl_robots_agent: str = "BizFlowBot"
    # A crawl with no heartbeat for this long is considered dead and is failed by the reaper.
    crawl_stale_after_seconds: int = Field(default=900, ge=30)
    # Abuse limits per organization.
    crawl_min_interval_seconds: int = Field(default=60, ge=0)
    knowledge_max_sources_per_org: int = Field(default=10, ge=1)

    def __repr_args__(self):  # type: ignore[no-untyped-def]
        """Never show credentials embedded in a URL (the database and Redis passwords)."""
        for name, value in super().__repr_args__():
            if isinstance(value, str) and "://" in value:
                value = _URL_CREDENTIALS.sub(r"\g<head>:***@", value)
            yield name, value

    @field_validator("jwt_secret_key")
    @classmethod
    def _strong_secret(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < MIN_JWT_SECRET_LENGTH:
            raise ValueError(f"JWT_SECRET_KEY must be at least {MIN_JWT_SECRET_LENGTH} characters")
        return v

    @field_validator("environment", mode="before")
    @classmethod
    def _known_environment(cls, v: object) -> str:
        value = str(v).strip().lower()
        if value not in ENVIRONMENTS:
            raise ValueError(f"ENVIRONMENT must be one of: {', '.join(ENVIRONMENTS)}")
        return value

    @field_validator("llm_provider", "embedding_provider", mode="before")
    @classmethod
    def _provider_id(cls, v: object) -> str:
        value = str(v).strip().lower()
        if not _PROVIDER_ID.match(value):
            raise ValueError("provider ids are lowercase letters, digits and underscores")
        return value

    @field_validator("shopify_api_version")
    @classmethod
    def _shopify_version(cls, v: str) -> str:
        if not is_quarterly_version(v):
            raise ValueError(
                f"SHOPIFY_API_VERSION must be a quarterly version such as {CURRENT_API_VERSION}"
            )
        return v.strip()

    @model_validator(mode="after")
    def _consistent(self) -> "Settings":
        if self.environment.lower() == "production":
            blocked = non_production_provider_ids()
            for env_name, provider in (
                ("EMBEDDING_PROVIDER", self.embedding_provider),
                ("LLM_PROVIDER", self.llm_provider),
            ):
                if provider in blocked:
                    raise ValueError(
                        f"{env_name}={provider} is not allowed when ENVIRONMENT=production"
                    )
        if self.environment.lower() == "production":
            if self.shopify_dev_base_url:
                raise ValueError("SHOPIFY_DEV_BASE_URL is not allowed when ENVIRONMENT=production")
            if self.shopify_redirect_uri.startswith("http://") and self.shopify_client_id:
                raise ValueError("SHOPIFY_REDIRECT_URI must be https in production")
        if self.chunk_overlap >= self.chunk_size // 2:
            raise ValueError("CHUNK_OVERLAP must be smaller than half of CHUNK_SIZE")
        if not self.crawl_allowed_port_set:
            raise ValueError("CRAWL_ALLOWED_PORTS must list at least one port")
        return self

    @property
    def shopify_configured(self) -> bool:
        secret = (
            self.shopify_client_secret.get_secret_value().strip()
            if self.shopify_client_secret
            else ""
        )
        return bool(self.shopify_client_id.strip() and secret)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def docs_enabled(self) -> bool:
        return (
            self.api_docs_enabled if self.api_docs_enabled is not None else not self.is_production
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def crawl_allowed_port_set(self) -> frozenset[int]:
        ports = set()
        for part in self.crawl_allowed_ports.split(","):
            part = part.strip()
            if part:
                port = int(part)
                if not 0 < port < 65536:
                    raise ValueError("CRAWL_ALLOWED_PORTS contains an invalid port")
                ports.add(port)
        return frozenset(ports)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
