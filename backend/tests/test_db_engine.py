"""build_engine: the settings every connection gets. The PostgreSQL server-side limits are proven
against a real server in tests/integration/test_platform_hardening.py; this covers what needs none."""

from app.core.config import Settings
from app.db.database import build_engine


def settings(url: str) -> Settings:
    return Settings(_env_file=None, database_url=url, jwt_secret_key="k" * 40)


def test_bound_values_are_never_shown_in_errors_on_any_database():
    engine = build_engine(settings("sqlite://"))
    assert engine.hide_parameters is True
    engine.dispose()


def test_dead_connections_are_discarded_before_use():
    engine = build_engine(settings("sqlite://"))
    assert engine.pool._pre_ping is True
    engine.dispose()


def test_postgres_only_options_are_not_sent_to_other_databases():
    # SQLite would raise on an unknown connect argument such as `connect_timeout`/`options`.
    engine = build_engine(settings("sqlite://"))
    with engine.connect() as connection:
        assert connection.exec_driver_sql("select 1").scalar() == 1
    engine.dispose()


def test_a_postgres_engine_is_built_lazily_without_contacting_the_server():
    engine = build_engine(
        settings("postgresql+psycopg://u:p@203.0.113.1:5432/db")
    )  # TEST-NET, unreachable
    assert engine.dialect.name == "postgresql" and engine.pool._recycle == 1800
    engine.dispose()
