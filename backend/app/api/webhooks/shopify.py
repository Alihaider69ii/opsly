"""Public Shopify webhook endpoint. No login: authenticity is the HMAC-SHA256 signature Shopify puts
in ``X-Shopify-Hmac-Sha256``, computed over the raw body with the app's client secret.

The handler does no real work (Shopify gives it 5 seconds): it verifies, deduplicates, records the
event and queues a job, then answers 200. Anything slow happens in the worker.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import DbSession
from app.core.config import Settings, get_settings
from app.core.rate_limit import client_ip, limiter
from app.services.integrations.shopify.webhooks import WebhookRejected, receive_webhook
from app.workers.queue import JobQueue, get_job_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Queue = Annotated[JobQueue, Depends(get_job_queue)]

# Per client IP, only *failed* verifications count: Shopify itself is never throttled, but someone
# guessing at the endpoint is.
_BAD_SIGNATURE_LIMIT = 30
_BAD_SIGNATURE_WINDOW_SECONDS = 60


async def _read_body(request: Request, limit: int) -> bytes | None:
    """The raw body, or None if it exceeds ``limit`` (checked while streaming, not after)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


@router.post("/shopify")
async def shopify_webhook(
    request: Request, db: DbSession, settings: AppSettings, queue: Queue
) -> JSONResponse:
    body = await _read_body(request, settings.shopify_webhook_max_body_bytes)
    if body is None:
        return JSONResponse({"detail": "Payload too large"}, status_code=413)
    headers = {name.lower(): value for name, value in request.headers.items()}
    try:
        result = await run_in_threadpool(
            receive_webhook, db, body=body, headers=headers, settings=settings, queue=queue
        )
    except WebhookRejected as exc:
        if exc.status_code == 401 and not limiter.allow(
            f"shopify-webhook-bad:{client_ip(request)}",
            _BAD_SIGNATURE_LIMIT,
            _BAD_SIGNATURE_WINDOW_SECONDS,
        ):
            return JSONResponse({"detail": "Too many invalid requests"}, status_code=429)
        logger.info("shopify webhook rejected status=%d reason=%s", exc.status_code, exc.reason)
        return JSONResponse({"detail": exc.reason}, status_code=exc.status_code)
    return JSONResponse({"status": result.status}, status_code=200)
