"""Hardening that only means something against the real servers: PostgreSQL session limits and
parameter hiding, Redis heartbeat expiry, and the readiness endpoint over live dependencies."""

import time
import uuid

import pytest
import redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.db.database import build_engine, get_db
from app.main import app
from app.workers.queue import RedisJobQueue, get_job_queue
from tests.integration.conftest import TEST_DATABASE_URL, TEST_REDIS_URL

pytestmark = pytest.mark.integration


def settings(**overrides):
    return get_settings().model_copy(update={"database_url": TEST_DATABASE_URL, **overrides})


@pytest.fixture()
def hardened(pg_engine):
    engines = []

    def make(statement=1, lock=1, idle=2):
        engine = build_engine(
            settings(
                db_statement_timeout_seconds=statement,
                db_lock_timeout_seconds=lock,
                db_idle_in_transaction_timeout_seconds=idle,
            )
        )
        engines.append(engine)
        return engine

    yield make
    for engine in engines:
        engine.dispose()


# --- the engine --------------------------------------------------------------------------------------------------


def test_every_connection_gets_the_configured_server_side_limits(hardened):
    with hardened(statement=1, lock=3, idle=2).connect() as conn:
        shown = {
            name: conn.execute(text(f"SHOW {name}")).scalar_one()
            for name in ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout")
        }
    assert shown == {
        "statement_timeout": "1s",
        "lock_timeout": "3s",
        "idle_in_transaction_session_timeout": "2s",
    }


def test_a_runaway_statement_is_cancelled_by_the_server(hardened):
    started = time.monotonic()
    with hardened().connect() as conn, pytest.raises(DBAPIError) as exc:
        conn.execute(text("SELECT pg_sleep(10)"))
    assert time.monotonic() - started < 5
    assert "statement timeout" in str(exc.value.orig)


def test_a_contended_row_lock_gives_up_instead_of_waiting_for_ever(hardened, pg_engine):
    with pg_engine.begin() as setup:
        setup.execute(text("CREATE TABLE IF NOT EXISTS lock_probe (id int primary key)"))
        setup.execute(text("DELETE FROM lock_probe"))
        setup.execute(text("INSERT INTO lock_probe VALUES (1)"))
    try:
        with pg_engine.connect() as holder:
            holder.execute(text("SELECT * FROM lock_probe WHERE id = 1 FOR UPDATE"))
            with (
                hardened(statement=10, lock=1).connect() as waiter,
                pytest.raises(DBAPIError) as exc,
            ):
                waiter.execute(text("SELECT * FROM lock_probe WHERE id = 1 FOR UPDATE"))
            assert "lock timeout" in str(exc.value.orig)
            holder.rollback()
    finally:
        with pg_engine.begin() as cleanup:
            cleanup.execute(text("DROP TABLE IF EXISTS lock_probe"))


def test_a_client_that_vanishes_mid_transaction_cannot_hold_locks(hardened):
    with hardened(statement=10, lock=10, idle=2).connect() as conn:
        conn.execute(text("SELECT 1"))  # opens a transaction and then does nothing
        time.sleep(3)
        with pytest.raises(DBAPIError):
            conn.execute(text("SELECT 1"))  # the server has already terminated the session


def test_a_failed_statement_does_not_echo_its_bound_parameters(hardened):
    secret = "enc:v1:very-secret-ciphertext-" + uuid.uuid4().hex
    with hardened().connect() as conn, pytest.raises(DBAPIError) as exc:
        conn.execute(text("INSERT INTO table_that_does_not_exist (v) VALUES (:v)"), {"v": secret})
    assert secret not in str(exc.value) and secret not in repr(exc.value)
    assert "hide_parameters" in str(exc.value)


def test_postgres_own_error_text_echoes_values_so_the_log_formatter_must_redact_it(hardened):
    """``hide_parameters`` cannot stop the *server's* message from containing a value (a unique
    violation prints the key). Prove that, then prove the app's log handler removes it."""
    import io
    import logging

    from sqlalchemy.orm import Session

    from app.core.logging import configure_logging
    from app.db.models import User

    email = f"victim-{uuid.uuid4().hex[:8]}@example.com"
    with Session(hardened(statement=10, lock=10)) as session:
        session.add(User(email=email, password_hash="x", full_name="First"))
        session.commit()
        session.add(User(email=email, password_hash="x", full_name="Second"))
        with pytest.raises(DBAPIError) as exc:
            session.commit()
    assert email in str(exc.value.orig)  # the raw exception does carry the address...
    assert "parameters hidden" in str(exc.value)  # ...though the bound-parameter dump is suppressed
    with hardened().connect() as cleanup:
        cleanup.execute(text("DELETE FROM users WHERE email = :e"), {"e": email})
        cleanup.commit()

    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("app.test").error("save failed", exc_info=exc.value)
    out = stream.getvalue()
    assert "save failed" in out
    assert email not in out and "[redacted]" in out


def test_the_default_engine_uses_the_configured_limits(pg_engine):
    configured = settings()
    engine = build_engine(configured)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
        shown = conn.execute(text("SHOW statement_timeout")).scalar_one()
    engine.dispose()
    expected = configured.db_statement_timeout_seconds
    assert shown in {f"{expected}s", f"{expected // 60}min"}


# --- Redis heartbeat -------------------------------------------------------------------------------------------------


@pytest.fixture()
def queue():
    q = RedisJobQueue(TEST_REDIS_URL, key=f"bizflow:test:hb:{uuid.uuid4().hex}")
    yield q
    for key in q._client.scan_iter(match=f"{q._key}*"):
        q._client.delete(key)
    q.close()


def test_a_beating_worker_is_counted_and_a_silent_one_expires(queue):
    assert queue.live_workers() == 0
    queue.beat("a", 1)
    queue.beat("b", 30)
    assert queue.live_workers() == 2
    time.sleep(1.5)
    assert queue.live_workers() == 1  # "a" stopped beating (crashed or was killed) and fell off


def test_workers_of_another_deployment_sharing_the_redis_are_not_counted(queue):
    other = RedisJobQueue(TEST_REDIS_URL, key=queue._key + "-other")
    try:
        other.beat("x", 30)
        assert queue.live_workers() == 0
    finally:
        other._client.delete(f"{other._key}:worker:x")
        other.close()


def test_ping_is_true_when_reachable_and_false_without_raising_when_not(queue):
    assert queue.ping() is True
    dead = RedisJobQueue("redis://127.0.0.1:1/0", key="x", socket_timeout=0.2)
    assert dead.ping() is False
    dead.close()


def test_queue_operations_against_a_dead_redis_raise_a_redis_error_the_api_turns_into_a_503():
    dead = RedisJobQueue("redis://127.0.0.1:1/0", key="x", socket_timeout=0.2)
    with pytest.raises(redis.RedisError):
        dead.beat("w", 5)
    with pytest.raises(redis.RedisError):
        dead.live_workers()
    dead.close()


# --- readiness over the real servers ---------------------------------------------------------------------------------


def test_readiness_is_green_on_live_dependencies_and_goes_red_when_redis_is_unreachable(
    pg_client, redis_queue
):
    ok = pg_client.get("/health/ready")
    assert ok.status_code == 200 and ok.json()["checks"] == {"database": "ok", "redis": "ok"}

    dead = RedisJobQueue("redis://127.0.0.1:1/0", key="x", socket_timeout=0.2)
    app.dependency_overrides[get_job_queue] = lambda: dead
    try:
        down = pg_client.get("/health/ready")
        live = pg_client.get("/health/live")
    finally:
        dead.close()
    assert down.status_code == 503
    assert down.json()["checks"] == {"database": "ok", "redis": "unavailable"}
    assert live.status_code == 200  # liveness is unaffected by the outage


def test_readiness_goes_red_when_postgres_stops_answering(pg_client):
    broken = build_engine(settings(database_url="postgresql+psycopg://x:y@127.0.0.1:1/z"))
    from sqlalchemy.orm import Session

    def dead_db():
        with Session(broken) as session:
            yield session

    app.dependency_overrides[get_db] = dead_db
    try:
        r = pg_client.get("/health/ready")
    finally:
        broken.dispose()
    assert r.status_code == 503 and r.json()["checks"]["database"] == "unavailable"
    assert "127.0.0.1" not in r.text and "password" not in r.text.lower()
