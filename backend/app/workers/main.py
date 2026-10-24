"""Background worker. Run with:  python -m app.workers.main

A single-threaded loop: pop a job from Redis, run it, repeat. Job families:

* **crawl**: crawl a website into the knowledge base (Phase 2)
* **shopify_webhook**: process one stored Shopify webhook event (small, served first)
* **shopify_sync**: import (a time slice of) a Shopify store; long imports re-queue themselves

Run several processes for concurrency (a long import then never delays webhooks). Between jobs (and
at most once a minute) the worker also repairs failure modes: crawls and sync stages whose worker
died (no heartbeat) are recovered, work whose job was lost with Redis is re-queued, and recently
disconnected stores finish purging. SIGINT/SIGTERM finish the current page, save progress and exit.
"""

import logging
import signal
import threading
import time

import redis

from app.core.config import Settings, get_settings
from app.core.logging import SafeFormatter, quiet_http_clients
from app.core.request_context import RequestIdFilter, set_request_id
from app.core.secrets import SecretError, SecretManager
from app.core.startup_checks import enforce_startup
from app.db.database import SessionLocal
from app.services.ai.factory import build_embedding_provider
from app.services.crawler.crawler import CrawlLimits
from app.services.crawler.factory import build_fetcher, build_limits
from app.services.integrations.shopify import lifecycle, sync, webhooks
from app.services.integrations.shopify.factory import ShopifyRuntime, check_api_version
from app.services.integrations.shopify.subscriptions import SubscriptionAuditor
from app.services.knowledge.crawl_runner import (
    fail_stale_crawls,
    requeue_orphaned_pending,
    run_crawl,
)
from app.workers.heartbeat import WorkerHeartbeat
from app.workers.queue import CrawlJob, Job, RedisJobQueue, ShopifySyncJob, ShopifyWebhookJob

logger = logging.getLogger("bizflow.worker")

DEQUEUE_TIMEOUT_SECONDS = 5
MAINTENANCE_INTERVAL_SECONDS = 60
ORPHANED_PENDING_AFTER_SECONDS = 300
ORPHANED_EVENT_AFTER_SECONDS = 120
CRASH_BACKOFF_SECONDS = 2.0


def configure_worker_logging() -> None:
    root = logging.getLogger()
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())  # a job's log lines carry its id, like a request's
    handler.setFormatter(
        SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s%(request_suffix)s")
    )
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    quiet_http_clients()


def run_job(
    job: Job,
    *,
    settings: Settings,
    limits: CrawlLimits,
    runtime: ShopifyRuntime | None,
    queue: RedisJobQueue,
    stop: threading.Event,
) -> str:
    """Run one job and say how it ended. Anything a job raises is the caller's to contain."""
    if isinstance(job, CrawlJob):
        outcome = run_crawl(
            SessionLocal,
            job.source_id,
            settings=settings,
            limits=limits,
            fetcher_factory=lambda: build_fetcher(settings),
            embedder_factory=lambda: build_embedding_provider(settings),
            should_stop=stop.is_set,
        )
        logger.info("job finished source=%s outcome=%s", job.source_id, outcome)
        return outcome
    if runtime is None:
        logger.error("shopify job dropped: SECRET_ENCRYPTION_KEY is not usable")
        return "dropped"
    if isinstance(job, ShopifySyncJob):
        outcome = sync.run_sync_job(
            runtime, SessionLocal, job.connection_id, queue=queue, should_stop=stop.is_set
        )
        logger.info(
            "shopify sync job finished connection=%s outcome=%s", job.connection_id, outcome
        )
        return outcome
    if isinstance(job, ShopifyWebhookJob):
        outcome = webhooks.process_event(runtime, SessionLocal, job.event_id)
        logger.info("shopify webhook job finished event=%s outcome=%s", job.event_id, outcome)
        return outcome
    return "ignored"  # pragma: no cover (parse_job only produces the types above)


def job_label(job: Job) -> str:
    if isinstance(job, CrawlJob):
        return f"crawl:{job.source_id}"
    if isinstance(job, ShopifySyncJob):
        return f"sync:{job.connection_id}"
    if isinstance(job, ShopifyWebhookJob):
        return f"webhook:{job.event_id}"
    return "job"  # pragma: no cover


def process_next(
    queue: RedisJobQueue,
    *,
    settings: Settings,
    limits: CrawlLimits,
    runtime: ShopifyRuntime | None,
    stop: threading.Event,
) -> bool:
    """Wait for one job and run it. True if a job ran. Never raises: Redis being down, or a job
    crashing, is logged and followed by a pause, so the worker survives and the state the job left
    behind (a stage, an event, a source) is repaired by the maintenance loop."""
    try:
        job = queue.dequeue(timeout=DEQUEUE_TIMEOUT_SECONDS)
    except redis.RedisError:
        logger.warning("redis unavailable, retrying")
        stop.wait(2.0)
        return False
    if job is None:
        return False
    set_request_id(job_label(job))
    try:
        run_job(job, settings=settings, limits=limits, runtime=runtime, queue=queue, stop=stop)
    except Exception:
        # One bad job must never take the worker down; the pause stops a persistent failure, such
        # as a database outage, from spinning.
        logger.exception("job crashed job=%s", job_label(job))
        stop.wait(CRASH_BACKOFF_SECONDS)
    finally:
        set_request_id(None)
    return True


def main() -> None:
    configure_worker_logging()
    settings = get_settings()
    enforce_startup(settings)  # production: refuse to start with an unsafe configuration
    limits = build_limits(settings)
    queue = RedisJobQueue(
        settings.redis_url,
        key=settings.redis_queue_key,
        socket_timeout=DEQUEUE_TIMEOUT_SECONDS + 5.0,
    )
    stop = threading.Event()

    runtime: ShopifyRuntime | None = None
    try:
        runtime = ShopifyRuntime(settings=settings, secrets=SecretManager.from_settings(settings))
    except SecretError as exc:
        logger.warning("shopify jobs disabled: secret manager not usable code=%s", exc.code)
    if runtime is not None and settings.shopify_configured:
        check_api_version(settings.shopify_api_version)

    def request_stop(signum: int, _frame: object) -> None:
        logger.info("shutdown requested (signal %s)", signum)
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    heartbeat = WorkerHeartbeat(queue)
    heartbeat.start()
    logger.info(
        "worker started id=%s embedding=%s max_pages=%d shopify=%s",
        heartbeat.worker_id,
        settings.embedding_provider,
        limits.max_pages,
        "enabled" if runtime else "disabled",
    )
    auditor = SubscriptionAuditor(settings.shopify_webhook_audit_interval_seconds)
    last_maintenance = 0.0
    try:
        while not stop.is_set():
            if time.monotonic() - last_maintenance >= MAINTENANCE_INTERVAL_SECONDS:
                last_maintenance = time.monotonic()
                maintain(settings, queue, runtime, auditor)
            process_next(queue, settings=settings, limits=limits, runtime=runtime, stop=stop)
    finally:
        heartbeat.stop()
        queue.close()
    logger.info("worker stopped")


def maintain(
    settings: Settings,
    queue: RedisJobQueue,
    runtime: ShopifyRuntime | None,
    auditor: SubscriptionAuditor | None = None,
) -> None:
    """Periodic repairs. Each step is independent: one failing never blocks the others."""
    steps = [
        lambda: fail_stale_crawls(SessionLocal, settings.crawl_stale_after_seconds),
        lambda: requeue_orphaned_pending(SessionLocal, queue, ORPHANED_PENDING_AFTER_SECONDS),
        lambda: sync.reap_stale_sync_states(
            SessionLocal, queue, settings.shopify_sync_stale_after_seconds
        ),
        lambda: sync.requeue_orphaned_sync(SessionLocal, queue, ORPHANED_PENDING_AFTER_SECONDS),
        lambda: webhooks.requeue_pending_events(SessionLocal, queue, ORPHANED_EVENT_AFTER_SECONDS),
        lambda: webhooks.prune_old_events(SessionLocal, settings.shopify_webhook_retention_days),
        lambda: lifecycle.finish_pending_purges(SessionLocal),
    ]
    if runtime is not None:
        steps.append(lambda: lifecycle.rotate_aging_tokens(runtime, SessionLocal))
        if auditor is not None:
            steps.append(lambda: auditor.run(runtime, SessionLocal, queue).checked)
    for step in steps:
        try:
            step()
        except Exception:
            logger.exception("maintenance step failed")


if __name__ == "__main__":
    main()
