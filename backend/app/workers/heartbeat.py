"""Proof that a worker process is alive, and a command a container runtime can use to check it.

A worker spends minutes inside one job (a crawl, a store import), so "the main loop ticked
recently" would report a busy worker as dead. The heartbeat therefore runs in its own thread: it
proves the process is up and scheduled, and nothing more. A job that is stuck is detected by the
job's own heartbeat and the reaper (``crawl_runner`` and ``sync``), not by this.

Two signals, because they answer different questions:

* a **file** touched every few seconds: "is THIS worker container alive?", for the container's own
  HEALTHCHECK (``python -m app.workers.healthcheck``), which needs no network;
* a **Redis key with a short expiry**: "is ANY worker alive?", shown by ``GET /health/worker`` so
  monitoring notices when imports and webhooks have nobody to run them.
"""

import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

import redis

from app.workers.queue import JobQueue

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 5.0
REDIS_TTL_SECONDS = 20
DEFAULT_FILE = Path(tempfile.gettempdir()) / "bizflow-worker.heartbeat"


def heartbeat_file() -> Path:
    return Path(os.environ.get("WORKER_HEARTBEAT_FILE") or DEFAULT_FILE)


class WorkerHeartbeat:
    def __init__(
        self,
        queue: JobQueue,
        *,
        path: Path | None = None,
        interval: float = INTERVAL_SECONDS,
        worker_id: str | None = None,
    ) -> None:
        self._queue = queue
        self._path = path or heartbeat_file()
        self._interval = interval
        self.worker_id = worker_id or uuid.uuid4().hex[:12]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="worker-heartbeat", daemon=True)

    def beat_once(self) -> None:
        try:
            self._path.touch()
            os.utime(self._path)
        except OSError:
            logger.warning("worker heartbeat file could not be written")
        try:
            self._queue.beat(self.worker_id, REDIS_TTL_SECONDS)
        except (redis.RedisError, OSError):
            logger.warning("worker heartbeat could not reach redis")

    def _run(self) -> None:
        while not self._stop.is_set():
            self.beat_once()
            self._stop.wait(self._interval)

    def start(self) -> None:
        self.beat_once()  # alive from the first instant, not after one interval
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        try:
            self._path.unlink(missing_ok=True)  # a stopped worker must not look alive
        except OSError:
            pass


def file_is_fresh(path: Path, max_age_seconds: float, now: float | None = None) -> bool:
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    return age <= max_age_seconds
