"""Production refuses to start with an unsafe configuration; development stays frictionless."""

import logging
import secrets

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import ENVIRONMENTS, Settings
from app.core.startup_checks import (
    UnsafeConfiguration,
    check_configuration,
    enforce_startup,
)

JWT = secrets.token_urlsafe(48)
DB_PASSWORD = "Xk9-correct-horse-battery-staple"
KEY = "sk-live-ABCDEF0123456789-secret-do-not-print"


def production(**overrides) -> Settings:
    base = {
        "environment": "production",
        "database_url": f"postgresql+psycopg://app:{DB_PASSWORD}@db.internal:5432/bizflow?sslmode=require",
        "jwt_secret_key": JWT,
        "cors_origins": "https://app.bizflow.test",
        "redis_url": "rediss://:redis-pass@redis.internal:6379/0",
        "forwarded_allow_ips": "10.0.0.5",
        "llm_provider": "openai",
        "llm_api_key": SecretStr(KEY),
        "embedding_provider": "openai",
        "embedding_api_key": SecretStr(KEY),
        "rate_limit_backend": "redis",
    }
    return Settings(_env_file=None, **{**base, **overrides})


def problems(**overrides) -> list[str]:
    return check_configuration(production(**overrides), environ={}).problems


def test_shopify_oauth_rate_limit_defaults_to_twenty_and_is_bounded():
    assert production().shopify_oauth_rate_limit == 20
    assert production(shopify_oauth_rate_limit=100).shopify_oauth_rate_limit == 100
    with pytest.raises(ValidationError):
        production(shopify_oauth_rate_limit=0)


# --- a correct production configuration starts cleanly ------------------------------------------------


def test_a_correct_production_configuration_has_no_findings():
    findings = check_configuration(production(), environ={})
    assert findings.problems == [] and findings.warnings == []
    assert enforce_startup(production(), environ={}).problems == []


# --- every rule, one at a time ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # secrets
        ({"jwt_secret_key": "a" * 40}, "JWT_SECRET_KEY uses fewer than"),
        (
            {"jwt_secret_key": "please-changeme-" + secrets.token_urlsafe(30)},
            "looks like a placeholder",
        ),
        ({"jwt_secret_key": "test-only-secret-" + "x" * 64}, "JWT_SECRET_KEY"),
        # database
        ({"database_url": "sqlite:///./bizflow.db"}, "must be PostgreSQL"),
        (
            {"database_url": "postgresql+psycopg://bizflow:bizflow@db:5432/bizflow"},
            "well-known development password",
        ),
        ({"database_url": "postgresql+psycopg://app@db:5432/bizflow"}, "has no password"),
        # browsers
        ({"cors_origins": "*"}, "never a wildcard"),
        ({"cors_origins": "http://app.bizflow.test"}, "must be an https:// origin"),
        (
            {"cors_origins": "https://app.bizflow.test,http://localhost:3000"},
            "must be an https:// origin",
        ),
        ({"cors_origins": "https://localhost"}, "must not allow localhost"),
        ({"cors_origins": "https://app.bizflow.test/some/path"}, "without a path"),
        ({"cors_origins": ""}, "CORS_ORIGINS is empty"),
        # infrastructure that must be a decision
        ({"forwarded_allow_ips": ""}, "FORWARDED_ALLOW_IPS must be set explicitly"),
        ({"redis_url": "memcached://x"}, "REDIS_URL must start with"),
        ({"log_level": "DEBUG"}, "LOG_LEVEL=DEBUG"),
    ],
)
def test_each_unsafe_setting_is_refused_with_a_reason_that_names_it(overrides, expected):
    found = problems(**overrides)
    assert any(expected in item for item in found), found


def test_redis_must_be_set_explicitly_not_inherited_from_the_development_default():
    settings = Settings(
        _env_file=None,
        environment="production",
        database_url=f"postgresql+psycopg://app:{DB_PASSWORD}@db.internal/bizflow",
        jwt_secret_key=JWT,
        cors_origins="https://app.bizflow.test",
        forwarded_allow_ips="10.0.0.5",
        llm_provider="openai",
        llm_api_key=SecretStr(KEY),
        embedding_provider="openai",
        embedding_api_key=SecretStr(KEY),
    )
    assert any(
        "REDIS_URL must be set explicitly" in p for p in check_configuration(settings, {}).problems
    )


def test_the_forwarded_peers_can_come_from_the_environment_variable_uvicorn_reads():
    settings = production(forwarded_allow_ips="")
    assert any("FORWARDED_ALLOW_IPS" in p for p in check_configuration(settings, {}).problems)
    assert not any(
        "FORWARDED_ALLOW_IPS" in p
        for p in check_configuration(settings, {"FORWARDED_ALLOW_IPS": "10.1.2.3"}).problems
    )


# --- Shopify ----------------------------------------------------------------------------------------------------------


SHOPIFY = {
    "shopify_client_id": "client-id",
    "shopify_client_secret": SecretStr("client-secret-value"),
    "secret_encryption_key": SecretStr("MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="),
    "shopify_webhook_base_url": "https://api.bizflow.test",
    "shopify_redirect_uri": "https://app.bizflow.test/integrations/shopify/callback",
}


def test_a_complete_shopify_configuration_is_accepted():
    assert problems(**SHOPIFY) == []


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"secret_encryption_key": None}, "SECRET_ENCRYPTION_KEY is unusable"),
        (
            {"secret_encryption_key": SecretStr("not-a-valid-key")},
            "SECRET_ENCRYPTION_KEY is unusable",
        ),
        ({"shopify_webhook_base_url": ""}, "SHOPIFY_WEBHOOK_BASE_URL"),
        ({"shopify_webhook_base_url": "http://api.bizflow.test"}, "must be an https:// URL"),
        ({"shopify_client_secret": None}, "must be set together"),
    ],
)
def test_shopify_in_production_needs_secure_storage_and_a_deliberate_webhook_decision(
    override, expected
):
    found = problems(**{**SHOPIFY, **override})
    assert any(expected in item for item in found), found


def test_running_without_live_updates_is_allowed_only_when_chosen_explicitly():
    config = {**SHOPIFY, "shopify_webhook_base_url": ""}
    assert problems(**config, shopify_webhooks_disabled=True) == []


def test_a_deployment_without_shopify_is_not_asked_about_it():
    assert problems() == []


# --- AI providers -------------------------------------------------------------------------------------------------------------


def test_a_provider_without_a_key_is_refused_at_start_not_at_the_first_customer_question():
    found = problems(llm_api_key=None, openai_api_key=None)
    assert any(item.startswith("LLM provider is not usable") for item in found)
    found = problems(embedding_api_key=None, openai_api_key=None)
    assert any(item.startswith("EMBEDDING provider is not usable") for item in found)


def test_the_fake_ai_provider_cannot_reach_production_by_any_route():
    with pytest.raises(ValidationError):
        production(llm_provider="fake")
    with pytest.raises(ValidationError):
        production(embedding_provider="fake")
    smuggled = production().model_copy(update={"llm_provider": "fake"})  # bypassing validation
    assert any(
        "LLM provider is not usable" in p for p in check_configuration(smuggled, {}).problems
    )


# --- warnings are findings that do not stop a start ------------------------------------------------------------------------


def test_doubtful_but_workable_settings_warn_without_blocking():
    findings = check_configuration(
        production(
            rate_limit_backend="memory",
            forwarded_allow_ips="*",
            database_url=f"postgresql+psycopg://app:{DB_PASSWORD}@db.internal:5432/bizflow",
            redis_url="redis://redis.internal:6379/0",
            api_docs_enabled=True,
        ),
        environ={},
    )
    assert findings.problems == []
    text = " | ".join(findings.warnings)
    for fragment in (
        "RATE_LIMIT_BACKEND=memory",
        "FORWARDED_ALLOW_IPS=*",
        "no sslmode",
        "without a password",
        "not TLS",
        "API_DOCS_ENABLED",
    ):
        assert fragment in text, fragment


# --- refusing to start ----------------------------------------------------------------------------------------------------------


def test_every_problem_is_reported_in_one_refusal_and_no_secret_value_is_in_it(caplog):
    bad = production(
        jwt_secret_key="a" * 40,
        database_url="postgresql+psycopg://bizflow:bizflow@db:5432/bizflow",
        cors_origins="*",
        forwarded_allow_ips="",
        log_level="DEBUG",
    )
    caplog.set_level(logging.WARNING)
    with pytest.raises(UnsafeConfiguration) as exc:
        enforce_startup(bad, environ={})
    message = str(exc.value)
    assert len(exc.value.problems) >= 5 and all(p in message for p in exc.value.problems)
    for secret in (JWT, DB_PASSWORD, KEY, "a" * 40, "bizflow:bizflow"):
        assert secret not in message and secret not in caplog.text
    assert "Refusing to start" in message


def test_outside_production_nothing_is_enforced_so_development_stays_frictionless():
    dev = Settings(
        _env_file=None,
        database_url="sqlite://",
        jwt_secret_key="k" * 40,
        llm_provider="fake",
        embedding_provider="fake",
    )
    assert enforce_startup(dev, environ={}).problems == []


def test_staging_logs_what_production_would_refuse_but_still_starts(caplog):
    staging = Settings(
        _env_file=None,
        environment="staging",
        database_url="sqlite://",
        jwt_secret_key="k" * 40,
        llm_provider="fake",
        embedding_provider="fake",
    )
    caplog.set_level(logging.WARNING)
    findings = enforce_startup(staging, environ={})
    assert findings.problems
    assert "would be refused in production" in caplog.text


# --- the environment name itself ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["prod", "prd", "live", "Production2", "", "dev"])
def test_an_unknown_environment_name_is_refused_so_it_cannot_silently_skip_the_guards(name):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, database_url="sqlite://", jwt_secret_key="k" * 40, environment=name
        )


@pytest.mark.parametrize("name", ["PRODUCTION", " production ", "Staging"])
def test_the_environment_name_is_normalized(name):
    s = Settings(
        _env_file=None,
        database_url="sqlite://",
        jwt_secret_key="k" * 40,
        environment=name,
        llm_provider="openai",
        embedding_provider="openai",
    )
    assert s.environment == name.strip().lower() and s.environment in ENVIRONMENTS


# --- the entry points really call the gate -----------------------------------------------------------------------------------------


def test_the_api_refuses_to_start_unsafely(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "get_settings", lambda: production(cors_origins="*"))
    with pytest.raises(UnsafeConfiguration):
        main.create_app()


def test_the_api_serves_no_interactive_docs_in_production_and_does_in_development(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "get_settings", lambda: production())
    prod_app = main.create_app()
    assert (prod_app.docs_url, prod_app.redoc_url, prod_app.openapi_url) == (None, None, None)

    dev = Settings(_env_file=None, database_url="sqlite://", jwt_secret_key="k" * 40)
    monkeypatch.setattr(main, "get_settings", lambda: dev)
    dev_app = main.create_app()
    assert dev_app.docs_url == "/docs" and dev_app.openapi_url == "/openapi.json"


def test_the_worker_refuses_to_start_unsafely(monkeypatch):
    from app.workers import main as worker

    monkeypatch.setattr(worker, "get_settings", lambda: production(forwarded_allow_ips=""))
    monkeypatch.setattr(worker, "configure_worker_logging", lambda: None)
    monkeypatch.setattr(
        worker,
        "RedisJobQueue",
        lambda *_args, **_kwargs: pytest.fail("worker proceeded past the production startup gate"),
    )
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    with pytest.raises(UnsafeConfiguration):
        worker.main()
