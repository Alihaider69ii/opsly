"""Job queue: Redis lists carrying tiny JSON jobs.

Redis is only a delivery mechanism. The source of truth for a job is a database row (a
``knowledge_sources`` row for crawls, ``shopify_sync_states`` / ``shopify_webhook_events`` rows for
Shopify), and a worker *claims* a job with an atomic database update, so:

* duplicate deliveries are harmless (the second claim matches no row and is skipped);
* a job lost with Redis is recovered by the worker's reaper (orphaned PENDING sources re-queued);
* a worker that dies mid-job is detected by its missing heartbeat (stale work is failed or retried).

That keeps the queue trivial: no acknowledgements, no visibility timeouts, no Celery.

There is one list per job family, read in priority order: Shopify webhooks (tiny, and Shopify
expects a quick turnaround) before crawls before Shopify syncs (long, and time-sliced so they
re-queue themselves between slices).
"""

import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

CRAWL_QUEUE_KEY = "bizflow:queue:crawl"
SHOPIFY_WEBHOOK_SUFFIX = ":shopify-webhook"
SHOPIFY_SYNC_SUFFIX = ":shopify-sync"


@dataclass(frozen=True)
class CrawlJob:
    source_id: uuid.UUID

    def to_json(self) -> str:
        return json.dumps({"type": "crawl", "source_id": str(self.source_id)})

    @classmethod
    def from_json(cls, raw: str) -> "CrawlJob | None":
        try:
            data = json.loads(raw)
            if data.get("type") != "crawl":
                return None
            return cls(uuid.UUID(data["source_id"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            return None


@dataclass(frozen=True)
class ShopifySyncJob:
    """Run (or continue) the import for one Shopify connection."""

    connection_id: uuid.UUID

    def to_json(self) -> str:
        return json.dumps({"type": "shopify_sync", "connection_id": str(self.connection_id)})


@dataclass(frozen=True)
class ShopifyWebhookJob:
    """Process one stored webhook event."""

    event_id: uuid.UUID

    def to_json(self) -> str:
        return json.dumps({"type": "shopify_webhook", "event_id": str(self.event_id)})


Job = CrawlJob | ShopifySyncJob | ShopifyWebhookJob


def parse_job(raw: str) -> Job | None:
    """A job from its JSON form; None for anything malformed or of an unknown type."""
    try:
        data = json.loads(raw)
        kind = data.get("type")
        if kind == "crawl":
            return CrawlJob(uuid.UUID(data["source_id"]))
        if kind == "shopify_sync":
            return ShopifySyncJob(uuid.UUID(data["connection_id"]))
        if kind == "shopify_webhook":
            return ShopifyWebhookJob(uuid.UUID(data["event_id"]))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return None


class JobQueue(Protocol):
    def enqueue(self, job: Job) -> None: ...

    def dequeue(self, timeout: float = 5.0) -> Job | None: ...

    def close(self) -> None: ...

    def ping(self) -> bool: ...

    def beat(self, worker_id: str, ttl_seconds: int) -> None: ...

    def live_workers(self) -> int: ...


class RedisJobQueue:
    def __init__(
        self, url: str, *, key: str = CRAWL_QUEUE_KEY, socket_timeout: float = 2.0
    ) -> None:
        self._key = key
        # Highest priority first: BLPOP serves the first non-empty list in this order.
        self._keys = [key + SHOPIFY_WEBHOOK_SUFFIX, key, key + SHOPIFY_SYNC_SUFFIX]
        self._client: redis.Redis = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=socket_timeout,
        )

    def _key_for(self, job: Job) -> str:
        if isinstance(job, ShopifyWebhookJob):
            return self._key + SHOPIFY_WEBHOOK_SUFFIX
        if isinstance(job, ShopifySyncJob):
            return self._key + SHOPIFY_SYNC_SUFFIX
        return self._key

    def enqueue(self, job: Job) -> None:
        self._client.rpush(self._key_for(job), job.to_json())

    def dequeue(self, timeout: float = 5.0) -> Job | None:
        item = self._client.blpop(self._keys, timeout=max(1, int(timeout)))
        if item is None:
            return None
        job = parse_job(item[1])  # type: ignore[index]
        if job is None:
            logger.warning("discarding malformed job payload")
        return job

    def length(self) -> int:
        """Jobs waiting across every list."""
        total = 0
        for key in self._keys:
            total += int(self._client.llen(key))  # type: ignore[arg-type]
        return total

    def keys(self) -> list[str]:
        return list(self._keys)

    def ping(self) -> bool:
        """Is Redis reachable right now? Never raises (used by the readiness probe)."""
        try:
            return bool(self._client.ping())
        except redis.RedisError:
            return False

    def beat(self, worker_id: str, ttl_seconds: int) -> None:
        """Record that a worker process is alive; the key expires by itself if it stops beating."""
        self._client.set(f"{self._key}:worker:{worker_id}", str(time.time()), ex=ttl_seconds)

    def live_workers(self) -> int:
        return sum(1 for _ in self._client.scan_iter(match=f"{self._key}:worker:*", count=100))

    def close(self) -> None:
        self._client.close()


class InMemoryJobQueue:
    """For tests (and single-process experiments). Same interface, no Redis."""

    def __init__(self) -> None:
        self.jobs: deque[Job] = deque()
        self.fail_enqueue = False
        self.workers: dict[str, int] = {}

    def enqueue(self, job: Job) -> None:
        if self.fail_enqueue:
            raise redis.ConnectionError("queue unavailable (test)")
        self.jobs.append(job)

    def dequeue(self, timeout: float = 0.0) -> Job | None:
        return self.jobs.popleft() if self.jobs else None

    def ping(self) -> bool:
        return not self.fail_enqueue

    def beat(self, worker_id: str, ttl_seconds: int) -> None:
        self.workers[worker_id] = ttl_seconds

    def live_workers(self) -> int:
        return len(self.workers)

    def close(self) -> None:
        pass


@lru_cache
def get_job_queue() -> JobQueue:
    """FastAPI dependency / producer-side queue (short socket timeout so an API request can't hang
    on a dead Redis)."""
    settings = get_settings()
    return RedisJobQueue(settings.redis_url, key=settings.redis_queue_key)
