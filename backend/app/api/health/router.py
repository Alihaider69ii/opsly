"""Liveness, readiness and worker health: three different questions, three different endpoints.

* ``GET /health/live`` (and ``GET /health``): is this process running? Touches nothing else, so a
  database outage cannot make an orchestrator restart healthy API processes in a loop.
* ``GET /health/ready``: can this instance serve requests? PostgreSQL and Redis must both answer;
  otherwise 503, and a load balancer stops sending it traffic. This is what a deployment's
  readiness probe should call.
* ``GET /health/worker``: has any background worker beaten in the last few seconds? Not part of
  readiness (the API serves fine without one) but what monitoring alerts on, because with no worker
  Shopify imports, webhooks and crawls silently stop.
* ``GET /health/db``: the original database probe, kept for existing callers.

Bodies name a component and a state, never an address, a version, a credential or an exception.
"""

import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.workers.queue import JobQueue, get_job_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
@router.get("/live")
def live() -> dict[str, str]:
    """Liveness: the process is up. Does not touch the database or Redis."""
    return {"status": "ok"}


def _database_ok(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any failure means "not ready", and the cause is logged
        logger.warning("readiness: database check failed")
        db.rollback()
        return False


def _redis_ok(queue: JobQueue) -> bool:
    try:
        return bool(queue.ping())
    except Exception:  # noqa: BLE001
        logger.warning("readiness: redis check failed")
        return False


@router.get("/ready")
def ready(
    response: Response, db: Session = Depends(get_db), queue: JobQueue = Depends(get_job_queue)
) -> dict[str, object]:
    checks = {
        "database": "ok" if _database_ok(db) else "unavailable",
        "redis": "ok" if _redis_ok(queue) else "unavailable",
    }
    healthy = all(state == "ok" for state in checks.values())
    if not healthy:
        response.status_code = 503
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ready" if healthy else "unavailable", "checks": checks}


@router.get("/worker")
def worker(response: Response, queue: JobQueue = Depends(get_job_queue)) -> dict[str, object]:
    try:
        workers = queue.live_workers()
    except Exception:  # noqa: BLE001
        logger.warning("worker health: redis check failed")
        workers = -1
    state = "ok" if workers > 0 else ("unknown" if workers < 0 else "none")
    if state != "ok":
        response.status_code = 503
    response.headers["Cache-Control"] = "no-store"
    return {"status": state, "workers": max(workers, 0)}


@router.get("/db")
def health_db(response: Response, db: Session = Depends(get_db)) -> dict[str, str]:
    """Readiness of the database only (kept for existing callers; prefer /health/ready)."""
    if not _database_ok(db):
        response.status_code = 503
        return {"status": "unavailable", "database": "unavailable"}
    return {"status": "ok", "database": "ok"}
