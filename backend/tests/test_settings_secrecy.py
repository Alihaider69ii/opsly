"""A Settings object can end up in a log line, an exception message or a CI failure report by
accident; none of those may reveal a credential."""

import io
import logging

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.logging import configure_logging

JWT = "jwt-signing-key-" + "Zq8" * 12
DB_PASSWORD = "Db-P4ssw0rd-never-print"
REDIS_PASSWORD = "Redis-P4ss-never-print"
KEYS = {
    "llm_api_key": "sk-llm-" + "a1B2" * 8,
    "embedding_api_key": "sk-emb-" + "c3D4" * 8,
    "openai_api_key": "sk-oai-" + "e5F6" * 8,
    "shopify_client_secret": "shpss_" + "0f" * 16,
    "secret_encryption_key": "ZW5jcnlwdGlvbi1rZXktMzItYnl0ZXMtbG9uZyEhISE=",
    "secret_encryption_previous_keys": "cHJldmlvdXMta2V5LTMyLWJ5dGVzLWxvbmctISEhISE=",
}
EVERYTHING = [JWT, DB_PASSWORD, REDIS_PASSWORD, *KEYS.values()]


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="production",
        database_url=f"postgresql+psycopg://app:{DB_PASSWORD}@db.internal:5432/bizflow",
        redis_url=f"rediss://:{REDIS_PASSWORD}@redis.internal:6379/0",
        jwt_secret_key=JWT,
        cors_origins="https://app.bizflow.test",
        llm_provider="openai",
        embedding_provider="openai",
        **{name: SecretStr(value) for name, value in KEYS.items()},
    )


def test_repr_and_str_show_no_credential(settings):
    for rendered in (repr(settings), str(settings), f"{settings!r}", f"{settings}"):
        for secret in EVERYTHING:
            assert secret not in rendered, secret
    assert "db.internal" in repr(settings)  # the host stays: useful and not a secret


def test_the_url_userinfo_is_masked_but_the_user_name_is_kept(settings):
    assert "postgresql+psycopg://app:***@db.internal" in repr(settings)
    assert "rediss://:***@redis.internal" in repr(settings)


def test_the_signing_key_is_a_secret_value_not_a_string(settings):
    assert isinstance(settings.jwt_secret_key, SecretStr)
    assert JWT not in str(settings.jwt_secret_key) and JWT not in repr(settings.jwt_secret_key)
    assert settings.jwt_secret_key.get_secret_value() == JWT


def test_a_dumped_settings_dictionary_does_not_expose_secret_fields(settings):
    dumped = str(settings.model_dump())
    for secret in (JWT, *KEYS.values()):
        assert secret not in dumped, secret


def test_logging_the_settings_object_cannot_leak(settings):
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("app.test").info("starting with %s", settings)
    logging.getLogger("app.test").error("bad config", extra={"settings": settings})
    for secret in EVERYTHING:
        assert secret not in stream.getvalue(), secret


def test_a_validation_error_for_a_short_signing_key_does_not_echo_the_key():
    with pytest.raises(ValueError) as exc:
        Settings(_env_file=None, database_url="sqlite://", jwt_secret_key="short-secret")
    assert "short-secret" not in str(exc.value)
    assert "at least 32" in str(exc.value)
