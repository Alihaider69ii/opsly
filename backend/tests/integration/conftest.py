"""Integration tier: real PostgreSQL (pgvector) and real Redis from docker-compose.

Run with:  docker compose up -d db redis  &&  pytest -m integration

The tier never touches development data: it works in its own databases (`bizflow_test`, recreated
from scratch every session by running the real Alembic migrations) and its own Redis keys. If the
services are not running the tests FAIL with instructions, rather than silently skipping.
"""

import os
import uuid
from pathlib import Path

import pytest
import redis
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.db.database import get_db
from app.main import app
from app.workers.queue import RedisJobQueue, get_job_queue
from tests.helpers import create_org, signup_and_login

BACKEND_DIR = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://bizflow:bizflow@localhost:5432/bizflow_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
HINT = "Start the services first:  docker compose up -d db redis"


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "app" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def recreate_database(url: str) -> None:
    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    except OperationalError as exc:
        pytest.fail(f"PostgreSQL is not reachable at {target.host}:{target.port}. {HINT}\n{exc}")
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def pg_engine() -> Engine:
    recreate_database(TEST_DATABASE_URL)
    command.upgrade(
        alembic_config(TEST_DATABASE_URL), "head"
    )  # the REAL migrations build the schema
    engine = create_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture()
def pg_factory(pg_engine: Engine) -> sessionmaker[Session]:
    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE users, organizations RESTART IDENTITY CASCADE"))
    return sessionmaker(bind=pg_engine, expire_on_commit=False)


@pytest.fixture()
def pg(pg_factory) -> Session:
    with pg_factory() as session:
        yield session


@pytest.fixture()
def redis_queue() -> RedisJobQueue:
    key = f"bizflow:test:{uuid.uuid4().hex}"
    queue = RedisJobQueue(TEST_REDIS_URL, key=key, socket_timeout=10)
    try:
        queue._client.ping()
    except redis.RedisError as exc:
        pytest.fail(f"Redis is not reachable at {TEST_REDIS_URL}. {HINT}\n{exc}")
    yield queue
    queue._client.delete(*queue.keys())
    queue.close()


@pytest.fixture()
def integration_settings() -> Settings:
    return get_settings().model_copy(update={"crawl_min_interval_seconds": 0})


@pytest.fixture()
def pg_client(pg_factory, redis_queue, integration_settings) -> TestClient:
    """The real app, backed by real Postgres and a real Redis queue; one DB session per request."""

    def session_per_request():
        with pg_factory() as session:
            yield session

    app.dependency_overrides[get_db] = session_per_request
    app.dependency_overrides[get_job_queue] = lambda: redis_queue
    app.dependency_overrides[get_settings] = lambda: integration_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def two_tenants(pg_client):
    made = []
    for email, name in (("a@example.com", "Brand A"), ("b@example.com", "Brand B")):
        headers = signup_and_login(pg_client, email)
        made.append((headers, create_org(pg_client, headers, name)))
    return made
