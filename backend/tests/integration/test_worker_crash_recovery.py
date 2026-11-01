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
    WhatsAppConnection,
    WhatsAppConsentCategory,
    WhatsAppConsentStatus,
    WhatsAppMessage,
    WhatsAppWebhookEvent,
    WorkflowStatus,
)
from app.services import automation
from app.services.integrations.shopify import sync, webhooks
from app.services.integrations.shopify.factory import ShopifyRuntime
from app.services.integrations.shopify.validation import sign_webhook_body
from app.services.messaging.whatsapp import consent as wa_consent
from app.services.messaging.whatsapp import webhooks as wa_webhooks
from app.services.messaging.whatsapp import window as wa_window
from app.services.messaging.whatsapp.contacts import (
    get_or_create_contact as wa_get_or_create_contact,
)
from app.services.messaging.whatsapp.validation import sign_meta_body
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
from tests.whatsapp.conftest import seed_connection as seed_whatsapp_connection

pytestmark = pytest.mark.integration

KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
SECRET = "test-client-secret"
WHATSAPP_APP_SECRET = "test-whatsapp-app-secret"
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
                # WhatsApp shares this same deployment's secret-storage key; only the failure-
                # injection tests below exercise it, but every Stack gets a usable configuration.
                "whatsapp_messaging_provider": "fake",
                "whatsapp_app_secret": SecretStr(WHATSAPP_APP_SECRET),
                "whatsapp_webhook_verify_token": SecretStr("test-verify-token"),
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
            "WHATSAPP_MESSAGING_PROVIDER": "fake",
            "WHATSAPP_APP_SECRET": WHATSAPP_APP_SECRET,
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


# --- WhatsApp: real worker / Postgres / Redis failure injection (Phase 7) -------------------------
#
# Reuses the same ``Stack`` (its real worker-process management, real Postgres/Redis, the shared
# org) rather than building a parallel WhatsApp-specific harness -- exactly how the generic
# automation-action tests above already reuse it without needing Shopify-specific state. These
# complement the Phase 6 unit-level simulated crash test
# (tests/whatsapp/test_automation_integration.py::test_worker_crash_during_a_whatsapp_send_fails_the_execution_rather_than_retrying)
# with a REAL hard-killed OS process / REAL severed Postgres connection / a REAL (if unreachable)
# Redis client, per this project's "verify with real infrastructure, not just fakes" standard.


def _seed_whatsapp(stack) -> tuple[uuid.UUID, uuid.UUID]:
    """A CONNECTED connection, a contact OPTED_IN to SUPPORT, and an open customer-service
    window -- the minimum a ``send_whatsapp_text`` action needs to pass every policy gate."""
    secrets = SecretManager.from_settings(stack.settings)
    connection_id = seed_whatsapp_connection(
        stack.pg, stack.org_id, secrets, phone_number_id=f"PNID_{uuid.uuid4().hex[:10]}"
    )
    now = datetime.now(UTC)
    with stack.pg() as db:
        contact = wa_get_or_create_contact(
            db, organization_id=stack.org_id, phone_e164="+14155550100"
        )
        db.commit()
        contact_id = contact.id
        wa_consent.record_consent(
            db,
            organization_id=stack.org_id,
            contact_id=contact_id,
            category=WhatsAppConsentCategory.SUPPORT,
            status=WhatsAppConsentStatus.OPTED_IN,
            source="test",
            now=now,
        )
        db.commit()
        conversation = wa_window.get_or_create_conversation(
            db, organization_id=stack.org_id, contact_id=contact_id, connection_id=connection_id
        )
        wa_window.record_inbound(conversation, now=now, window_hours=24)
        db.commit()
    return connection_id, contact_id


def _whatsapp_automation_execution(stack, contact_id: uuid.UUID, action: dict) -> uuid.UUID:
    execution_id = uuid.uuid4()
    with stack.pg() as db:
        workflow = AutomationWorkflow(
            organization_id=stack.org_id,
            name=f"WA recovery {execution_id}",
            status=WorkflowStatus.ACTIVE,
            version=1,
            definition={
                "trigger": {"event": "whatsapp_message_received"},
                "conditions": [],
                "actions": [action],
            },
        )
        event = AutomationEvent(
            organization_id=stack.org_id,
            event_type="whatsapp_message_received",
            source="whatsapp",
            source_id=str(execution_id),
            dedupe_key=f"wa-recovery:{execution_id}",
            occurred_at=datetime.now(UTC),
            payload={"whatsapp": {"contact_id": str(contact_id)}},
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
            event_snapshot={"whatsapp": {"contact_id": str(contact_id)}},
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


def _hold_whatsapp_message_insert(stack) -> None:
    with stack.pg() as db:
        db.execute(
            text(
                "CREATE OR REPLACE FUNCTION test_pause_whatsapp_message() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(120); RETURN NEW; END $$"
            )
        )
        db.execute(text("DROP TRIGGER IF EXISTS test_pause_whatsapp_message ON whatsapp_messages"))
        db.execute(
            text(
                "CREATE TRIGGER test_pause_whatsapp_message BEFORE INSERT ON whatsapp_messages "
                "FOR EACH ROW EXECUTE FUNCTION test_pause_whatsapp_message()"
            )
        )
        db.commit()


def _remove_whatsapp_message_insert_hold(stack) -> None:
    with stack.pg() as db:
        # A hard-killed or network-severed client cannot send PostgreSQL a cancel while its
        # backend is in pg_sleep; cancel that test-only query directly so dropping the trigger
        # does not wait for the full 120s.
        db.execute(
            text(
                "SELECT pg_cancel_backend(pid) FROM pg_stat_activity "
                "WHERE datname=current_database() AND wait_event='PgSleep' "
                "AND query ILIKE '%INSERT INTO whatsapp_messages%'"
            )
        )
        db.execute(text("DROP TRIGGER IF EXISTS test_pause_whatsapp_message ON whatsapp_messages"))
        db.execute(text("DROP FUNCTION IF EXISTS test_pause_whatsapp_message()"))
        db.commit()


def _whatsapp_insert_is_waiting(stack) -> bool:
    with stack.pg() as db:
        return bool(
            db.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                    "AND wait_event='PgSleep' AND query ILIKE '%INSERT INTO whatsapp_messages%')"
                )
            )
        )


def _age_execution_heartbeat(stack, execution_id: uuid.UUID) -> None:
    """Pretend the killed worker stopped heart-beating an hour ago (mirrors
    ``Stack.age_running_stages`` for Shopify) so ``recover_due``'s staleness check fires
    immediately instead of waiting out its real 120s default."""
    with stack.pg() as db:
        db.execute(
            text(
                "UPDATE automation_executions SET heartbeat_at = now() - interval '1 hour' "
                "WHERE id = :id"
            ),
            {"id": str(execution_id)},
        )
        db.commit()


def _execution_status(stack, execution_id: uuid.UUID) -> ExecutionStatus:
    with stack.pg() as db:
        return db.get(AutomationExecution, execution_id).status


def _execution_error_code(stack, execution_id: uuid.UUID) -> str | None:
    with stack.pg() as db:
        return db.get(AutomationExecution, execution_id).error_code


def _whatsapp_message_count(stack) -> int:
    with stack.pg() as db:
        return int(
            db.scalar(
                select(func.count())
                .select_from(WhatsAppMessage)
                .where(WhatsAppMessage.organization_id == stack.org_id)
            )
        )


def test_killed_before_a_whatsapp_send_ever_reaches_the_provider_fails_the_execution_not_a_retry(
    stack,
):
    """Worker killed while a ``send_whatsapp_text`` step is RUNNING, before the message row (and
    therefore before any provider call) ever committed. This is an EXTERNAL_CALL_ACTION_TYPE, so
    crash recovery must fail the execution outright rather than silently retry a call that may (or
    may not) have already reached a real recipient -- see automation.py's ``recover_due`` and
    module docstring."""
    _, contact_id = _seed_whatsapp(stack)
    execution_id = _whatsapp_automation_execution(
        stack,
        contact_id,
        {"type": "send_whatsapp_text", "content": "Hello", "consent_category": "SUPPORT"},
    )
    _hold_whatsapp_message_insert(stack)
    first = stack.worker("whatsapp-killed")
    try:
        eventually(
            lambda: _whatsapp_insert_is_waiting(stack),
            what="the worker to enter the whatsapp_messages insert",
        )
        first.kill()
        assert _execution_status(stack, execution_id) == ExecutionStatus.RUNNING
        assert _whatsapp_message_count(stack) == 0  # no row ever committed -- no send happened
    finally:
        _remove_whatsapp_message_insert_hold(stack)

    _age_execution_heartbeat(stack, execution_id)
    second = stack.worker("whatsapp-recovery")
    eventually(
        lambda: _execution_status(stack, execution_id) == ExecutionStatus.FAILED,
        what="crash recovery to fail the execution rather than silently retry an external call",
    )
    assert _execution_error_code(stack, execution_id) == "worker_lost_during_whatsapp_send"
    assert _whatsapp_message_count(stack) == 0  # still never sent: fail-closed, not retried
    assert second.alive


def test_postgres_outage_during_a_whatsapp_send_recovers_without_a_duplicate_send(stack):
    """The worker is never killed here -- its connection to PostgreSQL is severed (a real network
    partition, not a mock) while it is inside the same whatsapp_messages insert held above. Unlike
    a hard process kill, the transaction that was building the message row never committed (the
    insert itself is what failed), so nothing was ever created or sent; the still-alive worker's
    normal retry of the same execution/step (same idempotency key: ``automation:<execution
    id>:<step index>``) finds no existing row and completes the send cleanly once PostgreSQL
    returns -- exactly once, never a duplicate delivery."""
    _, contact_id = _seed_whatsapp(stack)
    execution_id = _whatsapp_automation_execution(
        stack,
        contact_id,
        {
            "type": "send_whatsapp_text",
            "content": "Hello after outage",
            "consent_category": "SUPPORT",
        },
    )
    _hold_whatsapp_message_insert(stack)
    proxy = stack.proxy("localhost", 5432)
    database_url = TEST_DATABASE_URL.replace("localhost:5432", f"127.0.0.1:{proxy.port}")
    worker = stack.worker("whatsapp-db-outage", DATABASE_URL=database_url)
    try:
        eventually(
            lambda: _whatsapp_insert_is_waiting(stack),
            what="the worker to enter the whatsapp_messages insert",
        )
        proxy.cut()
        eventually(
            lambda: "job crashed" in worker.logs(),
            what="the database failure to be contained by the worker",
        )
        assert worker.alive
        proxy.restore()
    finally:
        _remove_whatsapp_message_insert_hold(stack)

    # A safety net in case the surviving worker's own retry is slower than the staleness window:
    # recover_due would also pick this up once the heartbeat goes stale, via a second worker.
    _age_execution_heartbeat(stack, execution_id)
    recovery = stack.worker("whatsapp-db-recovery")
    eventually(
        lambda: _execution_status(stack, execution_id) == ExecutionStatus.COMPLETED,
        what="the execution to complete once PostgreSQL is reachable again",
    )
    assert _whatsapp_message_count(stack) == 1  # sent exactly once, never duplicated
    assert worker.alive and recovery.alive


def _wa_inbound_payload(phone_number_id: str, wamid: str, wa_id: str, text_body: str) -> bytes:
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-test",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": phone_number_id},
                                "contacts": [{"profile": {"name": "Test"}, "wa_id": wa_id}],
                                "messages": [
                                    {
                                        "from": wa_id,
                                        "id": wamid,
                                        "timestamp": str(int(datetime.now(UTC).timestamp())),
                                        "type": "text",
                                        "text": {"body": text_body},
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }
    ).encode()


def test_a_duplicate_webhook_processed_by_a_real_worker_never_runs_the_automation_twice(stack):
    """The intake-level dedupe (unit-tested already) plus a REAL worker process consuming the
    resulting job(s): two identical deliveries must still produce exactly one ledger row, one
    inbound message, and one automation side effect -- not two."""
    connection_id, _ = _seed_whatsapp(stack)
    with stack.pg() as db:
        phone_number_id = db.get(WhatsAppConnection, connection_id).phone_number_id

    workflow_name = f"WA dup {uuid.uuid4().hex[:8]}"
    with stack.pg() as db:
        db.add(
            AutomationWorkflow(
                organization_id=stack.org_id,
                name=workflow_name,
                status=WorkflowStatus.ACTIVE,
                version=1,
                definition={
                    # The seeded contact has never messaged before, so its first inbound message
                    # is "whatsapp_conversation_started", not "whatsapp_message_received".
                    "trigger": {"event": "whatsapp_conversation_started"},
                    "conditions": [],
                    "actions": [{"type": "create_task", "title": "React to inbound WhatsApp"}],
                },
            )
        )
        db.commit()

    worker = stack.worker("whatsapp-dup-webhook")
    wamid = f"wamid.dup.{uuid.uuid4().hex[:10]}"
    body = _wa_inbound_payload(phone_number_id, wamid, "14155550199", "Duplicate delivery test")
    headers = {"x-hub-signature-256": sign_meta_body(body, WHATSAPP_APP_SECRET)}

    with stack.pg() as db:
        for _ in range(2):
            wa_webhooks.receive_webhook(
                db, body=body, headers=headers, settings=stack.settings, queue=stack.queue
            )

    def _state():
        with stack.pg() as db:
            ledger = int(
                db.scalar(
                    select(func.count())
                    .select_from(WhatsAppWebhookEvent)
                    .where(WhatsAppWebhookEvent.resource_wamid == wamid)
                )
            )
            messages = int(
                db.scalar(
                    select(func.count())
                    .select_from(WhatsAppMessage)
                    .where(WhatsAppMessage.provider_message_id == wamid)
                )
            )
            tasks = int(
                db.scalar(
                    select(func.count())
                    .select_from(AutomationTask)
                    .join(
                        AutomationExecution, AutomationExecution.id == AutomationTask.execution_id
                    )
                    .join(
                        AutomationWorkflow, AutomationWorkflow.id == AutomationExecution.workflow_id
                    )
                    .where(AutomationWorkflow.name == workflow_name)
                )
            )
            return ledger, messages, tasks

    eventually(
        lambda: _state() == (1, 1, 1),
        what="exactly one ledger row, inbound message and automation task despite two deliveries",
    )
    assert _state() == (1, 1, 1)
    assert worker.alive


def test_redis_unavailable_during_webhook_intake_rejects_cleanly_with_no_orphaned_ledger_row(
    stack,
):
    """A REAL (if deliberately unreachable) Redis client, not a mock: proves the actual
    ``redis.RedisError`` the real client raises is what ``_ledger``'s broad except clause catches,
    and that intake fails closed -- deletes its own just-written ledger row and tells the caller
    503 -- rather than leaving a half-written row Meta will never know to retry correctly."""
    connection_id, _ = _seed_whatsapp(stack)
    with stack.pg() as db:
        phone_number_id = db.get(WhatsAppConnection, connection_id).phone_number_id

    wamid = f"wamid.redisdown.{uuid.uuid4().hex[:10]}"
    body = _wa_inbound_payload(
        phone_number_id, wamid, "14155550188", "Should be rejected, not half-stored"
    )
    headers = {"x-hub-signature-256": sign_meta_body(body, WHATSAPP_APP_SECRET)}

    dead_queue = RedisJobQueue("redis://127.0.0.1:1/0", key="x", socket_timeout=0.2)
    try:
        with stack.pg() as db, pytest.raises(wa_webhooks.WebhookRejected) as exc:
            wa_webhooks.receive_webhook(
                db, body=body, headers=headers, settings=stack.settings, queue=dead_queue
            )
        assert exc.value.status_code == 503
    finally:
        dead_queue.close()

    with stack.pg() as db:
        orphaned = int(
            db.scalar(
                select(func.count())
                .select_from(WhatsAppWebhookEvent)
                .where(WhatsAppWebhookEvent.resource_wamid == wamid)
            )
        )
    assert orphaned == 0
