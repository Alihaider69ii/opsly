from collections.abc import Iterator
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings


def build_engine(settings: Settings) -> Engine:
    """The one place an engine is configured.

    * ``hide_parameters``: a failed statement's error message normally includes the bound parameter
      values, which here could be encrypted tokens or a customer's email; never put them in a log.
    * PostgreSQL gets a connect timeout and server-side limits on statement time, lock waits and
      idle-in-transaction time, so a stuck query, a contended row or a crashed client can never hold
      a connection (or a row lock) for ever. ``pool_pre_ping`` discards connections the database
      has dropped, ``pool_recycle`` retires old ones before a firewall or proxy does.
    """
    kwargs: dict[str, Any] = {"pool_pre_ping": True, "hide_parameters": True}
    if make_url(settings.database_url).get_backend_name() == "postgresql":
        limits = {
            "statement_timeout": settings.db_statement_timeout_seconds * 1000,
            "lock_timeout": settings.db_lock_timeout_seconds * 1000,
            "idle_in_transaction_session_timeout": settings.db_idle_in_transaction_timeout_seconds
            * 1000,
        }
        kwargs["connect_args"] = {
            "connect_timeout": settings.db_connect_timeout_seconds,
            "options": " ".join(f"-c {name}={value}" for name, value in limits.items()),
        }
        kwargs["pool_recycle"] = 1800
    return create_engine(settings.database_url, **kwargs)


engine = build_engine(get_settings())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    with SessionLocal() as session:
        yield session


def get_session_factory() -> sessionmaker[Session]:
    """FastAPI dependency for code that opens its own short transactions (background-style work
    run from a request, such as disconnecting a store)."""
    return SessionLocal
