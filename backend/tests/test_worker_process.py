"""The worker loop: one bad job cannot kill it, Redis can vanish under it, it stops cleanly on
SIGTERM, and everything it does is traceable to a job id."""

import logging
import signal
import threading
import uuid

import pytest
import redis

from app.core.config import get_settings
from app.core.request_context import current_request_id
from app.services.crawler.factory import build_limits
from app.workers import main as worker
from app.workers.queue import (
    CrawlJob,
    InMemoryJobQueue,
    ShopifySyncJob,
    ShopifyWebhookJob,
)


@pytest.fixture()
def quick(monkeypatch):
    monkeypatch.setattr(worker, "CRASH_BACKOFF_SECONDS", 0.0)


@pytest.fixture()
def ctx():
    settings = get_settings()
    return {
        "settings": settings,
        "limits": build_limits(settings),
        "runtime": None,
        "stop": threading.Event(),
    }


class ScriptedQueue(InMemoryJobQueue):
    def __init__(self, *results):
        super().__init__()
        self.results = list(results)
        self.closed = False

    def dequeue(self, timeout: float = 0.0):
        result = self.results.pop(0) if self.results else None
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


# --- dispatch -----------------------------------------------------------------------------------------------------


def test_each_job_type_goes_to_its_handler_with_its_own_arguments(monkeypatch, ctx):
    calls = []
    monkeypatch.setattr(
        worker,
        "run_crawl",
        lambda factory, source_id, **kw: calls.append(("crawl", source_id)) or "completed",
    )
    monkeypatch.setattr(
        worker.sync,
        "run_sync_job",
        lambda rt, factory, cid, **kw: calls.append(("sync", cid)) or "completed",
    )
    monkeypatch.setattr(
        worker.webhooks,
        "process_event",
        lambda rt, factory, eid: calls.append(("webhook", eid)) or "processed",
    )
    source, connection, event = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    queue = InMemoryJobQueue()
    runtime = object()  # any truthy runtime: the handlers above ignore it
    for job in (CrawlJob(source), ShopifySyncJob(connection), ShopifyWebhookJob(event)):
        worker.run_job(job, queue=queue, **{**ctx, "runtime": runtime})
    assert calls == [("crawl", source), ("sync", connection), ("webhook", event)]


def test_shopify_jobs_are_dropped_visibly_when_secure_storage_is_not_usable(ctx, caplog):
    caplog.set_level(logging.ERROR)
    for job in (ShopifySyncJob(uuid.uuid4()), ShopifyWebhookJob(uuid.uuid4())):
        assert worker.run_job(job, queue=InMemoryJobQueue(), **ctx) == "dropped"
    assert "SECRET_ENCRYPTION_KEY is not usable" in caplog.text


# --- containment --------------------------------------------------------------------------------------------------------


def test_a_job_that_crashes_is_logged_and_the_worker_carries_on(monkeypatch, ctx, quick, caplog):
    monkeypatch.setattr(
        worker, "run_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    caplog.set_level(logging.ERROR)
    job = ShopifySyncJob(uuid.uuid4())
    assert worker.process_next(ScriptedQueue(job), **ctx) is True  # did not raise
    assert "job crashed" in caplog.text and str(job.connection_id) in caplog.text


def test_a_crash_is_followed_by_a_pause_so_a_persistent_failure_cannot_spin(monkeypatch, ctx):
    waits = []

    class Stop(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            return False

    monkeypatch.setattr(
        worker, "run_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    worker.process_next(ScriptedQueue(CrawlJob(uuid.uuid4())), **{**ctx, "stop": Stop()})
    assert waits == [worker.CRASH_BACKOFF_SECONDS] and worker.CRASH_BACKOFF_SECONDS > 0


def test_a_redis_outage_is_a_short_wait_not_a_crash(ctx, caplog):
    caplog.set_level(logging.WARNING)
    stop = threading.Event()
    stop.set()  # wait() returns at once
    assert (
        worker.process_next(ScriptedQueue(redis.ConnectionError("down")), **{**ctx, "stop": stop})
        is False
    )
    assert "redis unavailable" in caplog.text


def test_no_job_means_nothing_ran(ctx):
    assert worker.process_next(ScriptedQueue(), **ctx) is False


def test_log_lines_from_a_job_carry_the_jobs_id_and_it_is_cleared_afterwards(monkeypatch, ctx):
    seen = []
    monkeypatch.setattr(
        worker, "run_job", lambda *a, **k: seen.append(current_request_id()) or "ok"
    )
    job = ShopifyWebhookJob(uuid.uuid4())
    worker.process_next(ScriptedQueue(job), **ctx)
    assert seen == [f"webhook:{job.event_id}"] and current_request_id() is None


# --- the whole lifecycle --------------------------------------------------------------------------------------------------


def test_the_worker_stops_cleanly_on_sigterm_and_leaves_no_heartbeat_behind(monkeypatch, tmp_path):
    handlers = {}
    monkeypatch.setattr(worker.signal, "signal", lambda sig, fn: handlers.__setitem__(sig, fn))
    monkeypatch.setenv("WORKER_HEARTBEAT_FILE", str(tmp_path / "hb"))
    queue = ScriptedQueue()
    monkeypatch.setattr(worker, "RedisJobQueue", lambda *a, **k: queue)
    monkeypatch.setattr(worker, "configure_worker_logging", lambda: None)
    maintained = []
    monkeypatch.setattr(worker, "maintain", lambda *a, **k: maintained.append(1))
    steps = []

    def process(queue_, *, stop, **kwargs):
        steps.append(1)
        assert (tmp_path / "hb").exists()  # alive while working
        if len(steps) == 2:
            handlers[signal.SIGTERM](signal.SIGTERM, None)  # the orchestrator asks us to stop
        return True

    monkeypatch.setattr(worker, "process_next", process)
    worker.main()
    assert len(steps) == 2  # it finished the iteration in progress and exited
    assert queue.closed and maintained == [1]
    assert not (tmp_path / "hb").exists()  # a stopped worker must not look alive
    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
