import os

# Must be set before app modules import settings. The default test tier never touches Postgres.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "test-only-secret-" + "x" * 64
os.environ["EMBEDDING_PROVIDER"] = "fake"

from dataclasses import dataclass  # noqa: E402

import pytest  # noqa: E402
from argon2 import PasswordHasher  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Engine, create_engine, event  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.core import security  # noqa: E402
from app.core.config import Settings, get_settings  # noqa: E402
from app.core.rate_limit import limiter  # noqa: E402
from app.db import models  # noqa: E402, F401
from app.db.base import Base  # noqa: E402
from app.db.database import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.workers.queue import InMemoryJobQueue, get_job_queue  # noqa: E402
from tests.helpers import create_org, signup_and_login  # noqa: E402


def pytest_collection_modifyitems(items):
    for item in items:
        if "/integration/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    limiter.reset()


@pytest.fixture(autouse=True, scope="session")
def _fast_password_hashing():
    """Argon2 at production cost makes ~100 signups slow; the algorithm (argon2id) is unchanged."""
    security._hasher = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)


@pytest.fixture()
def engine() -> Engine:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        # SQLite ignores foreign keys by default; turn them on so cascades and the composite
        # (id, organization_id) keys that tie children to their tenant are enforced in tests.
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def db(session_factory) -> Session:
    with session_factory() as session:
        yield session


@pytest.fixture()
def queue() -> InMemoryJobQueue:
    return InMemoryJobQueue()


@pytest.fixture()
def app_settings() -> Settings:
    """The settings the API sees in tests. Mutate fields (e.g. app_settings.crawl_min_interval_seconds)
    inside a test to change behavior."""
    settings = get_settings().model_copy(update={"crawl_min_interval_seconds": 0})
    return settings


@pytest.fixture()
def client(db: Session, queue: InMemoryJobQueue, app_settings: Settings) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_job_queue] = lambda: queue
    app.dependency_overrides[get_settings] = lambda: app_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


@dataclass
class Tenant:
    email: str
    headers: dict[str, str]
    org: dict


@pytest.fixture()
def tenants(client: TestClient) -> tuple[Tenant, Tenant]:
    """Two fully separate businesses, each with its own owner."""
    made = []
    for email, org_name in (("a@example.com", "Brand A"), ("b@example.com", "Brand B")):
        headers = signup_and_login(client, email)
        made.append(Tenant(email, headers, create_org(client, headers, org_name)))
    return made[0], made[1]
