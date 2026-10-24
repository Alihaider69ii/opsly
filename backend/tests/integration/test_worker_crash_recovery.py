"""Kill the real worker at the worst moments and prove the system heals.

Each scenario starts ``python -m app.workers.main`` as its own process (real PostgreSQL, real Redis,
Shopify played by ``FakeShopifyHttpServer``), holds one request open so the process is known to be
mid-operation, then hard-kills it. A second worker is started afterwards and must finish the job
with no duplicates and no lost work. Time is advanced the honest way (rows are aged in the database,
as they would be after the minutes a real recovery takes), because the recovery thresholds are
minutes long.

Every worker's complete log is also checked at the end for secrets and personal data: these tests
double as an audit of what a real process writes.
"""

import base64
import json
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis
from pydantic import SecretStr
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.secrets import SecretManager
from app.db.models import (
    AutomationEvent,
    AutomationExecution,
    AutomationExecutionStep,
    AutomationTask,
    AutomationWorkflow,
    Customer,
    EventStatus,
    ExecutionStatus,
    Order,
    OrderItem,
    Organization,
    Product,
    ProductVariant,
    ShopifyConnection,
    ShopifyConnectionStatus,
    ShopifySyncState,
    ShopifyWebhookEvent,
    StepStatus,
    SyncResource,
    SyncStatus,
    WebhookEventStatus,
    WorkflowStatus,
)
from app.services import automation
from app.services.integrations.shopify import sync, webhooks
from app.services.integrations.shopify.factory import ShopifyRuntime
from app.services.integrations.shopify.validation import sign_webhook_body
from app.workers.queue import (
    AutomationExecutionJob,
    RedisJobQueue,
    ShopifySyncJob,
    ShopifyWebhookJob,
)
from tests.integration.conftest import TEST_DATABASE_URL, TEST_REDIS_URL
from tests.integration.process_harness import (
    TcpProxy,
    WorkerProcess,
    base_worker_env,
    eventually,
    still_true_for,
)
from tests.shopify.conftest import seed_connection
from tests.shopify.fake_shopify import FakeShopify, make_product, make_variant
from tests.shopify.http_server import FakeShopifyHttpServer, operation_is

pytestmark = pytest.mark.integration

KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
SECRET = "test-client-secret"
PRODUCTS, CUSTOMERS, ORDERS = 60, 30, 30  # 3 pages of products, 1 of customers, 2 of orders


class Stack:
    """One isolated deployment: its own Redis queue, the shared test database, a fake Shopify."""

    def __init__(self, pg_factory, tmp_path):
        self.pg = pg_factory
        self.tmp = tmp_path
        self.fake = FakeShopify()
        self.issued: list[str] = []
        original = self.fake._issue

        def recording_issue():
            body = original()
            self.issued.extend(v for k, v in body.items() if k in {"access_token", "refresh_token"})
            return body

        self.fake._issue = recording_issue
        self.server = FakeShopifyHttpServer(self.fake).start()
        self.server.seed(PRODUCTS, CUSTOMERS, ORDERS)
        self.queue_key = f"bizflow:test:kill:{uuid.uuid4().hex}"
        self.queue = RedisJobQueue(TEST_REDIS_URL, key=self.queue_key)
        self.workers: list[WorkerProcess] = []
        self.proxies: list[TcpProxy] = []
        settings = get_settings().model_copy(
            update={
                "secret_encryption_key": SecretStr(KEY),
                "shopify_client_secret": SecretStr(SECRET),
                "shopify_client_id": "test-client-id",
            }
        )
        self.settings = settings
        self.runtime = ShopifyRuntime(
            settings=settings, secrets=SecretManager.from_settings(settings)
        )
        with pg_factory() as db:
            org = Organization(name="Crash Co", slug=f"crash-{uuid.uuid4().hex[:8]}")
            db.add(org)
            db.commit()
            self.org_id = org.id
        self.cid = seed_connection(pg_factory, self.org_id, self.runtime, self.fake)

    # -- processes -------------------------------------------------------------------------------

    def env(self, **overrides: str) -> dict[str, str]:
        settings = {
            "DATABASE_URL": TEST_DATABASE_URL,
            "REDIS_URL": TEST_REDIS_URL,
            "REDIS_QUEUE_KEY": self.queue_key,
            "JWT_SECRET_KEY": "test-only-secret-" + "x" * 64,
            "SECRET_ENCRYPTION_KEY": KEY,
            "SHOPIFY_CLIENT_ID": "test-client-id",
            "SHOPIFY_CLIENT_SECRET": SECRET,
            "SHOPIFY_REDIRECT_URI": "http://localhost:3000/integrations/shopify/callback",
            "SHOPIFY_DEV_BASE_URL": self.server.base_url,
            "SHOPIFY_SYNC_MIN_INTERVAL_SECONDS": "0",
            "SHOPIFY_MAX_RETRIES": "2",
            "WORKER_HEARTBEAT_FILE": str(self.tmp / f"hb-{uuid.uuid4().hex[:6]}"),
        }
        return base_worker_env(**{**settings, **overrides})

    def worker(self, name: str, **env: str) -> WorkerProcess:
        proc = WorkerProcess(self.env(**env), self.tmp / f"{name}.log", name).start()
        self.workers.append(proc)
        proc.wait_started()
        return proc

    def proxy(self, host: str, port: int) -> TcpProxy:
        p = TcpProxy(host, port)
        self.proxies.append(p)
        return p

    # -- database views --------------------------------------------------------------------------

    def count(self, model) -> int:
        with self.pg() as db:
            return int(db.scalar(select(func.count()).select_from(model)))

    def states(self) -> dict[SyncResource, ShopifySyncState]:
        with self.pg() as db:
            rows = db.scalars(
                select(ShopifySyncState).where(ShopifySyncState.connection_id == self.cid)
            )
            return {s.resource: s for s in rows}

    def connection(self) -> ShopifyConnection:
        with self.pg() as db:
            return db.get(ShopifyConnection, self.cid)

    def synced(self) -> bool:
        states = self.states()
        return len(states) == 3 and all(s.status == SyncStatus.COMPLETED for s in states.values())

    def age_running_stages(self) -> None:
        """Pretend the killed worker stopped heart-beating a couple of hours ago."""
        with self.pg() as db:
            db.execute(
                text(
                    "UPDATE shopify_sync_states SET heartbeat_at = now() - interval '2 hours' "
                    "WHERE status = 'RUNNING'"
                )
            )
            db.commit()

    def start_sync(self) -> None:
        with self.pg() as db:
            connection = db.get(ShopifyConnection, self.cid)
            sync.prepare_sync(db, connection, datetime.now(UTC))
            db.commit()
            sync.enqueue_sync(db, connection, self.queue)

    def expect_everything_imported(self) -> None:
        assert self.count(Product) == PRODUCTS
        assert self.count(ProductVariant) == PRODUCTS * 2
        assert self.count(Customer) == CUSTOMERS
        assert self.count(Order) == ORDERS
        assert self.count(OrderItem) == ORDERS  # one line per order in the seed
        connection = self.connection()
        assert connection.status == ShopifyConnectionStatus.CONNECTED
        assert connection.error_code is None and connection.last_sync_at is not None

    # -- teardown --------------------------------------------------------------------------------

    def forbidden_in_logs(self) -> list[str]:
        return [*self.issued, SECRET, KEY]

    def close(self) -> list[str]:
        problems: list[str] = []
        for w in self.workers:
            w.kill()
            log = w.logs()
            for secret in self.forbidden_in_logs():
                if secret in log:
                    problems.append(f"{w.name} logged a secret ({secret[:6]}...)")
            if re.search(r"shopper\d+@private-example\.test|\+1555000\d{4}|Lovelace", log):
                problems.append(f"{w.name} logged customer personal data")
        for p in self.proxies:
            p.close()
        self.server.stop()
        for key in list(self.queue._client.scan_iter(match=f"{self.queue_key}*")):
            self.queue._client.delete(key)
        self.queue.close()
        return problems


@pytest.fixture()
def stack(pg_factory, tmp_path):
    s = Stack(pg_factory, tmp_path)
    yield s
    problems = s.close()
    assert problems == [], problems


# --- killed during each stage of the import -------------------------------------------------------------------


def test_killed_during_the_product_import_it_resumes_from_the_stored_cursor(stack):
    hold = stack.server.gate(operation_is("Products"), skip=1)  # hold the second page
    first = stack.worker("first")
    stack.start_sync()
    assert hold.wait_reached(60), "the worker never asked for a second page of products"

    # one page is already safely stored, with the cursor that continues after it
    state = stack.states()[SyncResource.PRODUCTS]
    assert (state.status, stack.count(Product)) == (SyncStatus.RUNNING, 20)
    assert state.cursor
    stored_cursor = state.cursor

    first.kill()
    hold.open()
    assert not first.alive
    assert still_true_for(lambda: not stack.synced(), 3)  # nothing finishes the job by itself

    stack.age_running_stages()
    stack.worker("second")
    eventually(stack.synced, what="the second worker to finish the import")

    stack.expect_everything_imported()
    pages = [s for s in stack.server.seen if s.operation == "Products"]
    assert sum(1 for s in pages if s.variables.get("after") is None) == 1  # never started over
    assert stored_cursor in [s.variables.get("after") for s in pages]  # continued from the cursor


def test_killed_during_the_customer_import_the_finished_product_stage_is_not_repeated(stack):
    hold = stack.server.gate(operation_is("Customers"))
    first = stack.worker("first")
    stack.start_sync()
    assert hold.wait_reached(60)
    states = stack.states()
    assert states[SyncResource.PRODUCTS].status == SyncStatus.COMPLETED
    assert states[SyncResource.CUSTOMERS].status == SyncStatus.RUNNING
    assert (stack.count(Product), stack.count(Customer)) == (PRODUCTS, 0)
    product_requests_before = stack.server.count("Products")

    first.kill()
    hold.open()
    stack.age_running_stages()
    stack.worker("second")
    eventually(stack.synced, what="the import to finish")

    stack.expect_everything_imported()
    assert stack.server.count("Products") == product_requests_before  # not fetched a second time


def test_killed_during_the_order_import_orders_and_their_lines_are_complete_and_unduplicated(stack):
    hold = stack.server.gate(operation_is("Orders"), skip=1)
    first = stack.worker("first")
    stack.start_sync()
    assert hold.wait_reached(60)
    assert stack.count(Order) == 20 and stack.states()[SyncResource.ORDERS].cursor
    first.kill()
    hold.open()
    stack.age_running_stages()
    stack.worker("second")
    eventually(stack.synced, what="the import to finish")
    stack.expect_everything_imported()


def kill_mid_products(stack, name: str, *, skip: int = 0) -> None:
    """Start a worker, hold one of its product requests, hard-kill it there, age its leftovers."""
    hold = stack.server.gate(operation_is("Products"), skip=skip)
    worker = stack.worker(name)
    assert hold.wait_reached(60), f"{name} never reached the product import"
    worker.kill()
    hold.open()
    stack.age_running_stages()


def test_two_kills_in_a_row_still_complete_on_the_third_attempt(stack):
    stack.start_sync()
    kill_mid_products(stack, "first", skip=1)  # dies after page 1 is stored
    kill_mid_products(stack, "second")  # dies on its very first request
    stack.worker("third")
    eventually(stack.synced, what="the third worker to finish the import")
    stack.expect_everything_imported()
    assert stack.states()[SyncResource.PRODUCTS].attempts == 3  # the documented limit, not beyond


def test_a_third_kill_fails_the_stage_visibly_and_a_manual_resync_recovers(stack):
    stack.start_sync()
    kill_mid_products(stack, "first", skip=1)
    kill_mid_products(stack, "second")
    kill_mid_products(stack, "third")
    stack.worker("fourth")
    failed = eventually(
        lambda: (s := stack.states()[SyncResource.PRODUCTS]).status == SyncStatus.FAILED and s,
        what="the stage to be failed after three interruptions",
    )
    # a safe, user-facing explanation from the fixed catalogue; never raw exception text
    assert failed.error_code == "interrupted"
    assert failed.error_message and "Traceback" not in failed.error_message
    assert not stack.synced()

    stack.start_sync()  # the merchant presses "Sync now"
    eventually(stack.synced, what="the manual re-sync to succeed")
    stack.expect_everything_imported()


# --- killed while processing a webhook ----------------------------------------------------------------------------


def deliver_webhook(stack, topic: str, payload: dict, webhook_id: str):
    body = json.dumps(payload).encode()
    headers = {
        "x-shopify-hmac-sha256": sign_webhook_body(body, SECRET),
        "x-shopify-topic": topic,
        "x-shopify-shop-domain": stack.fake.shop,
        "x-shopify-webhook-id": webhook_id,
    }
    with stack.pg() as db:
        return webhooks.receive_webhook(
            db, body=body, headers=headers, settings=stack.settings, queue=stack.queue
        )


def events(stack):
    with stack.pg() as db:
        return list(db.scalars(select(ShopifyWebhookEvent)))


def test_killed_while_processing_a_webhook_the_event_is_not_lost_and_is_applied_once(stack):
    stack.fake.products = [
        make_product(5, title="Fresh from Shopify", variants=[make_variant(501)])
    ]
    hold = stack.server.gate(operation_is("Product"))  # the single-product fetch
    first = stack.worker("first")
    result = deliver_webhook(stack, "products/update", {"id": 5, "title": "stale"}, "wh-kill-1")
    assert result.status == "accepted"
    assert hold.wait_reached(60)
    (event,) = events(stack)
    assert event.status == WebhookEventStatus.PROCESSING and event.attempts == 1
    assert stack.count(Product) == 0

    first.kill()
    hold.open()
    assert still_true_for(lambda: events(stack)[0].status == WebhookEventStatus.PROCESSING, 2)

    with stack.pg() as db:  # the minutes a real recovery waits
        db.execute(
            text("UPDATE shopify_webhook_events SET received_at = now() - interval '1 hour'")
        )
        db.commit()
    stack.worker("second")
    eventually(
        lambda: events(stack)[0].status == WebhookEventStatus.PROCESSED,
        what="the orphaned event to be processed",
    )
    assert stack.count(Product) == 1
    with stack.pg() as db:
        assert db.scalar(select(Product.title)) == "Fresh from Shopify"
    assert events(stack)[0].attempts == 2


def test_a_duplicate_delivery_and_duplicate_jobs_change_nothing(stack):
    stack.fake.products = [make_product(5, title="Once", variants=[make_variant(501)])]
    stack.worker("only")
    first = deliver_webhook(stack, "products/update", {"id": 5}, "wh-dup-1")
    assert first.status == "accepted"
    eventually(lambda: events(stack)[0].status == WebhookEventStatus.PROCESSED, what="processing")
    fetches = stack.server.count("Product")

    again = deliver_webhook(stack, "products/update", {"id": 5}, "wh-dup-1")  # Shopify retried
    assert again.status == "duplicate"
    event_id = events(stack)[0].id
    for _ in range(3):  # a lost-and-redelivered queue message, three times over
        stack.queue.enqueue(ShopifyWebhookJob(event_id))
    eventually(lambda: stack.queue.length() == 0, what="the duplicate jobs to be consumed")
    assert still_true_for(lambda: stack.server.count("Product") == fetches, 2)
    assert (stack.count(Product), len(events(stack)), events(stack)[0].attempts) == (1, 1, 1)


# --- killed while refreshing the access token --------------------------------------------------------------------


def test_killed_after_shopify_rotated_the_refresh_token_but_before_it_was_stored(stack):
    """The documented danger window: Shopify has issued new tokens, the worker dies before saving
    them. The stored refresh token must still work (Shopify's grace period), with no reconnect."""
    with stack.pg() as db:  # the access token has expired: the first call must refresh
        db.execute(
            text(
                "UPDATE shopify_connections SET access_token_expires_at = now() - interval '1 hour'"
            )
        )
        db.commit()
    before = stack.connection()
    hold = stack.server.gate(operation_is("token:refresh_token"), after=True)
    first = stack.worker("first")
    stack.start_sync()
    assert hold.wait_reached(60)
    assert (
        len(stack.fake.token_requests) == 1
        and stack.fake.token_requests[0]["grant_type"] == "refresh_token"
    )

    first.kill()
    hold.open()
    after_kill = stack.connection()
    assert after_kill.access_token_encrypted == before.access_token_encrypted  # nothing was saved
    assert after_kill.refresh_token_encrypted == before.refresh_token_encrypted
    assert after_kill.status != ShopifyConnectionStatus.ERROR

    stack.age_running_stages()
    stack.worker("second")
    eventually(stack.synced, what="the import to finish after re-refreshing with the old token")
    stack.expect_everything_imported()
    refreshes = [r for r in stack.fake.token_requests if r["grant_type"] == "refresh_token"]
    assert len(refreshes) == 2 and refreshes[0]["refresh_token"] == refreshes[1]["refresh_token"]
    assert stack.connection().error_code is None  # the merchant never had to reconnect


# --- several workers at once ------------------------------------------------------------------------------------------


def test_two_workers_and_duplicate_jobs_import_everything_exactly_once(stack):
    stack.worker("one")
    stack.worker("two")
    stack.start_sync()
    for _ in range(3):
        stack.queue.enqueue(ShopifySyncJob(stack.cid))
    eventually(stack.synced, what="the import to finish")
    stack.expect_everything_imported()
    assert stack.server.count("Products") == 3  # each page fetched once, not once per worker
    assert stack.server.count("Orders") == 2


# --- the infrastructure disappears ------------------------------------------------------------------------------------


def test_redis_going_away_does_not_kill_the_worker_and_work_resumes_when_it_returns(stack):
    proxy = stack.proxy("localhost", 6379)
    worker = stack.worker("w", REDIS_URL=f"redis://127.0.0.1:{proxy.port}/15")
    proxy.cut()
    assert still_true_for(lambda: worker.alive, 6)
    assert "redis unavailable" in worker.logs()
    proxy.restore()
    stack.start_sync()  # enqueued straight to the real Redis
    eventually(stack.synced, what="the worker to pick work up again after Redis returned")
    stack.expect_everything_imported()
    assert worker.alive


def test_postgres_going_away_mid_import_the_worker_survives_and_the_import_is_resumed(stack):
    proxy = stack.proxy("localhost", 5432)
    database_url = TEST_DATABASE_URL.replace("localhost:5432", f"127.0.0.1:{proxy.port}")
    hold = stack.server.gate(operation_is("Products"), skip=1)
    worker = stack.worker("w", DATABASE_URL=database_url)
    stack.start_sync()
    assert hold.wait_reached(60)
    proxy.cut()  # the database drops every connection while the worker is mid-import
    hold.open()
    eventually(lambda: "job crashed" in worker.logs(), what="the crash to be contained")
    assert worker.alive
    proxy.restore()
    stack.age_running_stages()
    eventually(stack.synced, timeout=150, what="the import to complete once the database returned")
    stack.expect_everything_imported()
    assert worker.alive


def _automation_execution(stack, actions: list[dict]) -> uuid.UUID:
    execution_id = uuid.uuid4()
    with stack.pg() as db:
        workflow = AutomationWorkflow(
            organization_id=stack.org_id,
            name=f"Recovery {execution_id}",
            status=WorkflowStatus.ACTIVE,
            version=1,
            definition={
                "trigger": {"event": "order_created"},
                "conditions": [],
                "actions": actions,
            },
        )
        event = AutomationEvent(
            organization_id=stack.org_id,
            event_type="order_created",
            source="internal",
            source_id=str(execution_id),
            dedupe_key=f"recovery:{execution_id}",
            occurred_at=datetime.now(UTC),
            payload={},
            depth=0,
            status=EventStatus.PROCESSED,
            attempts=1,
            created_at=datetime.now(UTC),
        )
        db.add_all([workflow, event])
        db.flush()
        execution = AutomationExecution(
            id=execution_id,
            organization_id=stack.org_id,
            workflow_id=workflow.id,
            event_id=event.id,
            workflow_version=workflow.version,
            workflow_snapshot={"name": workflow.name, "definition": workflow.definition},
            event_snapshot={},
            status=ExecutionStatus.QUEUED,
            current_step=0,
            attempts=0,
            max_attempts=5,
            created_at=datetime.now(UTC),
        )
        db.add(execution)
        db.commit()
    stack.queue.enqueue(AutomationExecutionJob(execution_id))
    return execution_id


def _hold_automation_task_insert(stack) -> None:
    with stack.pg() as db:
        db.execute(
            text(
                "CREATE OR REPLACE FUNCTION test_pause_automation_task() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(120); RETURN NEW; END $$"
            )
        )
        db.execute(text("DROP TRIGGER IF EXISTS test_pause_automation_task ON automation_tasks"))
        db.execute(
            text(
                "CREATE TRIGGER test_pause_automation_task BEFORE INSERT ON automation_tasks "
                "FOR EACH ROW EXECUTE FUNCTION test_pause_automation_task()"
            )
        )
        db.commit()


def _remove_automation_task_hold(stack) -> None:
    with stack.pg() as db:
        # A hard-killed client cannot send PostgreSQL a cancel while its backend is in pg_sleep.
        # Cancel that test-only query so dropping the trigger does not wait for the full 120 s.
        db.execute(
            text(
                "SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                "WHERE datname=current_database() AND wait_event='PgSleep' "
                "AND query ILIKE '%INSERT INTO automation_tasks%'"
            )
        )
        db.execute(text("DROP TRIGGER IF EXISTS test_pause_automation_task ON automation_tasks"))
        db.execute(text("DROP FUNCTION IF EXISTS test_pause_automation_task()"))
        db.commit()


def _task_insert_is_waiting(stack) -> bool:
    with stack.pg() as db:
        return bool(
            db.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                    "AND wait_event='PgSleep' AND query ILIKE '%INSERT INTO automation_tasks%')"
                )
            )
        )


def _automation_status(stack, execution_id: uuid.UUID) -> ExecutionStatus:
    with stack.pg() as db:
        return db.get(AutomationExecution, execution_id).status


def _automation_task_count(stack, execution_id: uuid.UUID) -> int:
    with stack.pg() as db:
        return int(
            db.scalar(
                select(func.count())
                .select_from(AutomationTask)
                .where(AutomationTask.execution_id == execution_id)
            )
        )


def test_killed_during_automation_action_recovers_without_duplicate_task(stack):
    execution_id = _automation_execution(stack, [{"type": "create_task", "title": "Exactly once"}])
    _hold_automation_task_insert(stack)
    first = stack.worker("automation-killed")
    try:
        eventually(
            lambda: _task_insert_is_waiting(stack),
            what="the automation worker to enter the task side effect",
        )
        first.kill()
        assert _automation_status(stack, execution_id) == ExecutionStatus.QUEUED
        assert _automation_task_count(stack, execution_id) == 0
    finally:
        _remove_automation_task_hold(stack)

    second = stack.worker("automation-recovery")
    eventually(
        lambda: _automation_status(stack, execution_id) == ExecutionStatus.COMPLETED,
        what="the recovered automation execution to complete",
    )
    assert _automation_task_count(stack, execution_id) == 1

    # Replaying a delivered job after commit is a no-op and cannot create a second task.
    stack.queue.enqueue(AutomationExecutionJob(execution_id))
    eventually(lambda: stack.queue.length() == 0, what="the duplicate execution job to be consumed")
    assert _automation_task_count(stack, execution_id) == 1
    assert second.alive


def test_automation_wait_survives_worker_restart_and_runs_once(stack):
    execution_id = _automation_execution(
        stack,
        [
            {"type": "wait", "seconds": 30},
            {"type": "create_task", "title": "After durable wait"},
        ],
    )
    first = stack.worker("automation-wait")
    eventually(
        lambda: _automation_status(stack, execution_id) == ExecutionStatus.WAITING,
        what="the first worker to persist the scheduled wait",
    )
    first.kill()
    with stack.pg() as db:
        execution = db.get(AutomationExecution, execution_id)
        step = db.scalar(
            select(AutomationExecutionStep).where(
                AutomationExecutionStep.execution_id == execution_id,
                AutomationExecutionStep.step_index == 0,
            )
        )
        assert step is not None and step.status == StepStatus.WAITING
        execution.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
        step.scheduled_at = execution.next_run_at
        db.commit()

    restarted = stack.worker("automation-after-wait")
    eventually(
        lambda: _automation_status(stack, execution_id) == ExecutionStatus.COMPLETED,
        what="the restarted worker to recover and finish the delayed execution",
    )
    assert _automation_task_count(stack, execution_id) == 1
    assert restarted.alive


def test_postgres_outage_during_automation_action_recovers_without_duplicate_task(stack):
    execution_id = _automation_execution(
        stack, [{"type": "create_task", "title": "After database returns"}]
    )
    _hold_automation_task_insert(stack)
    proxy = stack.proxy("localhost", 5432)
    database_url = TEST_DATABASE_URL.replace("localhost:5432", f"127.0.0.1:{proxy.port}")
    worker = stack.worker("automation-db-outage", DATABASE_URL=database_url)
    try:
        eventually(
            lambda: _task_insert_is_waiting(stack),
            what="the automation worker to enter the database side effect",
        )
        proxy.cut()
        eventually(
            lambda: "job crashed" in worker.logs(),
            what="the database failure to be contained by the worker",
        )
        assert worker.alive
        proxy.restore()
    finally:
        _remove_automation_task_hold(stack)

    assert _automation_status(stack, execution_id) == ExecutionStatus.QUEUED
    recovery = stack.worker("automation-db-recovery")
    eventually(
        lambda: _automation_status(stack, execution_id) == ExecutionStatus.COMPLETED,
        what="a worker to recover the queued execution after PostgreSQL returns",
    )
    assert _automation_task_count(stack, execution_id) == 1
    assert worker.alive and recovery.alive


def test_automation_event_persisted_during_redis_outage_is_recovered_from_postgres(stack):
    event_key = uuid.uuid4()
    with stack.pg() as db:
        workflow = AutomationWorkflow(
            organization_id=stack.org_id,
            name=f"Redis recovery {event_key}",
            status=WorkflowStatus.ACTIVE,
            version=1,
            definition={
                "trigger": {"event": "order_created"},
                "conditions": [],
                "actions": [{"type": "create_task", "title": "After Redis returns"}],
            },
        )
        db.add(workflow)
        db.commit()
        unavailable = RedisJobQueue(
            "redis://127.0.0.1:1/0", key=f"bizflow:test:offline:{event_key}", socket_timeout=0.2
        )
        try:
            with pytest.raises(redis.RedisError):
                automation.ingest_event(
                    db,
                    unavailable,
                    organization_id=stack.org_id,
                    event_type="order_created",
                    source="internal",
                    source_id=str(event_key),
                    dedupe_key=f"redis-outage:{event_key}",
                    payload={},
                )
        finally:
            unavailable.close()
        event = db.scalar(
            select(AutomationEvent).where(AutomationEvent.dedupe_key == f"redis-outage:{event_key}")
        )
        assert event is not None and event.status == EventStatus.PENDING
        event_id = event.id

    worker = stack.worker("automation-redis-recovery")
    eventually(
        lambda: (
            _automation_event_status(stack, event_id) == EventStatus.PROCESSED
            and _automation_execution_for_event(stack, event_id) == ExecutionStatus.COMPLETED
        ),
        what="the maintenance loop to requeue and execute the persisted event after Redis returns",
    )
    execution_id = _automation_execution_id(stack, event_id)
    assert _automation_task_count(stack, execution_id) == 1
    assert worker.alive


def _automation_event_status(stack, event_id: uuid.UUID) -> EventStatus:
    with stack.pg() as db:
        return db.get(AutomationEvent, event_id).status


def _automation_execution_for_event(stack, event_id: uuid.UUID) -> ExecutionStatus | None:
    with stack.pg() as db:
        return db.scalar(
            select(AutomationExecution.status).where(AutomationExecution.event_id == event_id)
        )


def _automation_execution_id(stack, event_id: uuid.UUID) -> uuid.UUID:
    with stack.pg() as db:
        return db.scalar(
            select(AutomationExecution.id).where(AutomationExecution.event_id == event_id)
        )


# --- stopping politely ---------------------------------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX signals; the handler itself is unit-tested"
)
def test_sigterm_lets_the_worker_finish_the_current_job_and_exit_cleanly(stack):
    import signal

    worker = stack.worker("w")
    stack.start_sync()
    eventually(lambda: stack.server.count("Products") >= 1, what="work to begin")
    worker.proc.send_signal(signal.SIGTERM)
    assert worker.wait_exit(60) == 0
    assert "worker stopped" in worker.logs()
    assert stack.states()[SyncResource.PRODUCTS].status != SyncStatus.FAILED
