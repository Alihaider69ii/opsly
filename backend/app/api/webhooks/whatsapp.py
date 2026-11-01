"""Public WhatsApp webhook endpoint. No login: the GET handshake proves the caller controls the
configured verify token; the POST body's authenticity is the ``X-Hub-Signature-256`` signature,
computed over the raw body with the Meta App secret.

The handler does no real work (acknowledge quickly): verify -> parse and bound -> deduplicate ->
record a ledger row -> enqueue -> answer. Anything slow (contact/conversation creation, the
automation event, an optional AI auto-reply) happens in the worker.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.deps import DbSession
from app.core.config import Settings, get_settings
from app.core.rate_limit import client_ip, limiter
from app.services.messaging.whatsapp.webhooks import (
    WebhookRejected,
    receive_webhook,
    verify_handshake,
)
from app.workers.queue import JobQueue, get_job_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Queue = Annotated[JobQueue, Depends(get_job_queue)]

# Per client IP, only *failed* verifications count: Meta itself is never throttled, but someone
# guessing at the endpoint or the verify token is.
_BAD_SIGNATURE_LIMIT = 30
_BAD_SIGNATURE_WINDOW_SECONDS = 60


@router.get("/whatsapp")
def whatsapp_verify(
    request: Request,
    settings: AppSettings,
    hub_mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    hub_verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    hub_challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> Response:
    configured = (
        settings.whatsapp_webhook_verify_token.get_secret_value()
        if settings.whatsapp_webhook_verify_token
        else ""
    )
    challenge = verify_handshake(
        mode=hub_mode, token=hub_verify_token, challenge=hub_challenge, configured_token=configured
    )
    if challenge is None:
        if not limiter.allow(
            f"whatsapp-webhook-bad:{client_ip(request)}",
            _BAD_SIGNATURE_LIMIT,
            _BAD_SIGNATURE_WINDOW_SECONDS,
        ):
            return JSONResponse({"detail": "Too many invalid requests"}, status_code=429)
        return JSONResponse({"detail": "verification failed"}, status_code=403)
    return PlainTextResponse(challenge)


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


@router.post("/whatsapp")
async def whatsapp_webhook(
    request: Request, db: DbSession, settings: AppSettings, queue: Queue
) -> JSONResponse:
    body = await _read_body(request, settings.whatsapp_webhook_max_body_bytes)
    if body is None:
        return JSONResponse({"detail": "Payload too large"}, status_code=413)
    headers = {name.lower(): value for name, value in request.headers.items()}
    try:
        results = await run_in_threadpool(
            receive_webhook, db, body=body, headers=headers, settings=settings, queue=queue
        )
    except WebhookRejected as exc:
        if exc.status_code == 401 and not limiter.allow(
            f"whatsapp-webhook-bad:{client_ip(request)}",
            _BAD_SIGNATURE_LIMIT,
            _BAD_SIGNATURE_WINDOW_SECONDS,
        ):
            return JSONResponse({"detail": "Too many invalid requests"}, status_code=429)
        logger.info("whatsapp webhook rejected status=%d reason=%s", exc.status_code, exc.reason)
        return JSONResponse({"detail": exc.reason}, status_code=exc.status_code)
    return JSONResponse({"status": "ok", "count": len(results)}, status_code=200)
