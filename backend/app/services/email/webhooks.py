"""Email provider webhooks: verification, intake and processing (mirrors
``app/services/messaging/whatsapp/webhooks.py``).

Unlike WhatsApp (one HTTP delivery can carry many events), Resend-style providers deliver one
JSON event per HTTP request: ``{"type": ..., "created_at": ..., "data": {...}}``. Two kinds
matter here:

* ``email.received`` -- an inbound email. The webhook carries metadata only (from/to/subject/
  ids); the body and headers are fetched separately by provider id
  (``EmailProvider.fetch_inbound``), exactly as Shopify's worker re-fetches a changed object by
  id rather than trusting a webhook body.
* a delivery-status event (sent/delivered/bounced/complained/delivery_delayed/failed/suppressed)
  -- applied to the already-stored outbound ``EmailMessage`` through the rank-checked state
  machine (``services/email/state.py``), so an out-of-order or duplicate delivery can never
  regress a message.

**Intake** does no real work: verify signature -> parse and bound the payload -> resolve the
organization (by inbound address for a received email; by the sent message's own row for a
status event) -> deduplicate -> store a ledger row -> enqueue -> answer quickly.
"""

import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Any, Literal

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import (
    AIHandoffState,
    EmailConnection,
    EmailDirection,
    EmailMessage,
    EmailMessageStatus,
    SenderType,
    SupportChannel,
    WebhookEventStatus,
    WhatsAppConnectionStatus,
)
from app.db.models.email import EmailWebhookEvent
from app.services.email import state as state_service
from app.services.email import threading as threading_service
from app.services.email.errors import EmailError
from app.services.email.provider import EmailProvider
from app.services.email.sanitize import sanitize_html
from app.services.email.validation import verify_svix_signature
from app.services.support import conversations as support_service
from app.services.support import identity as identity_service
from app.workers.queue import EmailWebhookJob, JobQueue

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
MAX_BODY_FIELD_CHARS = 4000

_STATUS_EVENT_TYPES = {
    "email.sent": "sent",
    "email.delivered": "delivered",
    "email.bounced": "bounced",
    "email.complained": "complained",
    "email.delivery_delayed": "delivery_delayed",
    "email.failed": "failed",
    "email.suppressed": "suppressed",
}


class WebhookRejected(Exception):
    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


@dataclass(frozen=True)
class IntakeResult:
    status: Literal["accepted", "duplicate", "ignored"]
    event_id: uuid.UUID | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)][:50]


def _connection_for_addresses(db: Session, addresses: list[str]) -> EmailConnection | None:
    if not addresses:
        return None
    lowered = {a.lower() for a in addresses}
    return db.scalar(
        select(EmailConnection)
        .where(
            EmailConnection.status != WhatsAppConnectionStatus.DISCONNECTED,
        )
        .where(EmailConnection.inbound_address.in_(lowered))
    )


def _connection_for_sent_message(db: Session, provider_message_id: str) -> EmailConnection | None:
    message = db.scalar(
        select(EmailMessage).where(EmailMessage.provider_message_id == provider_message_id)
    )
    if message is None:
        return None
    return db.scalar(
        select(EmailConnection).where(EmailConnection.organization_id == message.organization_id)
    )


def _ledger(
    db: Session,
    queue: JobQueue,
    *,
    dedupe_key: str,
    kind: str,
    organization_id: uuid.UUID | None,
    connection_id: uuid.UUID | None,
    payload: dict[str, Any],
    now: datetime,
) -> IntakeResult:
    event = EmailWebhookEvent(
        id=uuid.uuid4(),
        dedupe_key=dedupe_key,
        kind=kind,
        organization_id=organization_id,
        connection_id=connection_id,
        payload=payload,
        status=WebhookEventStatus.PENDING,
        received_at=now,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return IntakeResult("duplicate")
    try:
        queue.enqueue(EmailWebhookJob(event.id))
    except Exception:
        logger.exception("could not enqueue email webhook event=%s", event.id)
        db.execute(delete(EmailWebhookEvent).where(EmailWebhookEvent.id == event.id))
        db.commit()
        raise WebhookRejected(503, "queue unavailable") from None
    return IntakeResult("accepted", event.id)


def receive_webhook(
    db: Session,
    *,
    body: bytes,
    headers: Mapping[str, str],
    settings: Settings,
    queue: JobQueue,
    now: datetime | None = None,
) -> IntakeResult:
    """Handle one delivery. ``headers`` must have lower-cased names. Raises ``WebhookRejected``."""
    now = now or _utcnow()
    secret = (
        settings.email_webhook_secret.get_secret_value() if settings.email_webhook_secret else ""
    )
    if not verify_svix_signature(
        body=body,
        svix_id=headers.get("svix-id"),
        svix_timestamp=headers.get("svix-timestamp"),
        svix_signature=headers.get("svix-signature"),
        secret=secret,
    ):
        raise WebhookRejected(401, "invalid signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise WebhookRejected(400, "body is not JSON") from None
    if not isinstance(payload, dict):
        raise WebhookRejected(400, "body is not an object")

    event_type = _as_str(payload.get("type"))
    data = payload.get("data")
    if event_type is None or not isinstance(data, dict):
        return IntakeResult("ignored")
    created_at = _as_str(payload.get("created_at")) or now.isoformat()

    if event_type == "email.received":
        email_id = _as_str(data.get("email_id"))
        if not email_id:
            return IntakeResult("ignored")
        to_addresses = _as_str_list(data.get("to")) + _as_str_list(data.get("received_for"))
        connection = _connection_for_addresses(db, to_addresses)
        return _ledger(
            db,
            queue,
            dedupe_key=f"inbound:{email_id}",
            kind="inbound",
            organization_id=connection.organization_id if connection else None,
            connection_id=connection.id if connection else None,
            payload={
                "email_id": email_id,
                "message_id": _as_str(data.get("message_id")),
                "from": _as_str(data.get("from")),
                "to": to_addresses[:20],
                "cc": _as_str_list(data.get("cc"))[:20],
                "subject": (_as_str(data.get("subject")) or "")[:998],
            },
            now=now,
        )

    status = _STATUS_EVENT_TYPES.get(event_type)
    if status is None:
        return IntakeResult("ignored")  # opened/clicked/domain.*/contact.* etc: informational only
    email_id = _as_str(data.get("email_id"))
    if not email_id:
        return IntakeResult("ignored")
    connection = _connection_for_sent_message(db, email_id)
    raw_bounce = data.get("bounce")
    bounce: dict[str, Any] = raw_bounce if isinstance(raw_bounce, dict) else {}
    return _ledger(
        db,
        queue,
        dedupe_key=f"status:{email_id}:{status}:{created_at}",
        kind="status",
        organization_id=connection.organization_id if connection else None,
        connection_id=connection.id if connection else None,
        payload={
            "email_id": email_id,
            "status": status,
            "bounce_type": _as_str(bounce.get("type")),
            "bounce_message": (_as_str(bounce.get("message")) or "")[:300],
        },
        now=now,
    )


# --- processing --------------------------------------------------------------------------------


def _claim(db: Session, event_id: uuid.UUID) -> EmailWebhookEvent | None:
    result = db.execute(
        update(EmailWebhookEvent)
        .where(
            EmailWebhookEvent.id == event_id, EmailWebhookEvent.status == WebhookEventStatus.PENDING
        )
        .values(status=WebhookEventStatus.PROCESSING, attempts=EmailWebhookEvent.attempts + 1)
    )
    db.commit()
    if result.rowcount != 1:  # type: ignore[attr-defined]
        return None
    return db.get(EmailWebhookEvent, event_id)


def _finish(
    db: Session, event_id: uuid.UUID, status: WebhookEventStatus, error_code: str | None = None
) -> None:
    db.execute(
        update(EmailWebhookEvent)
        .where(EmailWebhookEvent.id == event_id)
        .values(
            status=status,
            error_code=error_code,
            processed_at=_utcnow() if status != WebhookEventStatus.PENDING else None,
        )
    )
    db.commit()


def _process_inbound(
    db: Session, event: EmailWebhookEvent, connection: EmailConnection, provider: EmailProvider
) -> str:
    payload = event.payload
    email_id = str(payload.get("email_id") or "")
    display_name, from_address = parseaddr(str(payload.get("from") or ""))
    from_address = from_address.strip().lower()
    if not from_address:
        _finish(db, event.id, WebhookEventStatus.IGNORED, "invalid_sender_address")
        return "ignored"
    now = _utcnow()
    try:
        content = provider.fetch_inbound(provider_message_id=email_id)
    except EmailError as exc:
        if exc.retryable and event.attempts < MAX_ATTEMPTS:
            return "retry"
        _finish(db, event.id, WebhookEventStatus.FAILED, f"fetch_failed:{exc.code.value}")
        return "failed"

    organization_id = connection.organization_id
    customer = identity_service.get_or_create_customer(
        db, organization_id=organization_id, email=from_address, name=display_name or None
    )
    thread = threading_service.resolve_inbound_thread(
        db,
        organization_id=organization_id,
        customer_id=customer.id,
        customer_email=from_address,
        subject=payload.get("subject"),
        provider_thread_id=None,
        in_reply_to_header=content.in_reply_to_header,
        references_header=content.references_header,
        now=now,
    )
    is_first_message = thread.message_count == 0
    message = EmailMessage(
        id=uuid.uuid4(),
        organization_id=organization_id,
        thread_id=thread.id,
        direction=EmailDirection.INBOUND,
        status=EmailMessageStatus.RECEIVED,
        idempotency_key=f"inbound:{email_id}",
        provider_message_id=email_id,
        message_id_header=content.message_id_header or payload.get("message_id"),
        in_reply_to_header=content.in_reply_to_header,
        references_header=content.references_header,
        from_address=from_address,
        to_addresses=_as_str_list(payload.get("to")),
        cc_addresses=_as_str_list(payload.get("cc")),
        subject=(payload.get("subject") or "")[:998] or None,
        body_text=content.text_body[: MAX_BODY_FIELD_CHARS * 10] if content.text_body else None,
        body_html_sanitized=sanitize_html(content.html_body),
        sender_type=SenderType.CUSTOMER,
    )
    is_retry = False
    db.add(message)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(EmailMessage).where(
                EmailMessage.organization_id == organization_id,
                EmailMessage.idempotency_key == f"inbound:{email_id}",
            )
        )
        if existing is None:
            _finish(db, event.id, WebhookEventStatus.PROCESSED)
            return "processed"
        message = existing
        is_retry = True

    if not is_retry:
        thread.last_inbound_at = now
        thread.message_count += 1
        db.commit()

        from app.services.automation import ingest_event
        from app.workers.queue import get_job_queue

        queue = get_job_queue()
        event_type = "email_conversation_started" if is_first_message else "email_message_received"
        ingest_event(
            db,
            queue,
            organization_id=organization_id,
            event_type=event_type,
            source="email",
            source_id=email_id,
            dedupe_key=f"email:message:{email_id}",
            payload={
                "event": {"source": "email"},
                "customer": {"email": from_address, "exists": True, "id": str(customer.id)},
                "email": {
                    "thread_id": str(thread.id),
                    "message_id": str(message.id),
                    "subject": (payload.get("subject") or "")[:200],
                },
            },
        )
        support_service.sync_conversation(
            db,
            organization_id=organization_id,
            channel=SupportChannel.EMAIL,
            channel_conversation_id=thread.id,
            ai_state=AIHandoffState(thread.ai_state.value),
            direction="inbound",
            sender_type=SenderType.CUSTOMER,
            event_type="email_inbound",
            customer_id=customer.id,
            message_preview=(message.body_text or "")[:300],
        )
        db.commit()

        if connection.ai_auto_reply_enabled and (message.body_text or "").strip():
            if event.attempts > MAX_ATTEMPTS:
                _finish(db, event.id, WebhookEventStatus.FAILED, "max_attempts_exceeded")
                return "failed"
            from app.services.email import autoreply
            from app.services.email.factory import get_email_provider

            try:
                autoreply_provider = get_email_provider()
            except EmailError:
                autoreply_provider = None
            if autoreply_provider is not None:
                outcome = autoreply.maybe_auto_reply(
                    db,
                    organization_id=organization_id,
                    connection=connection,
                    customer=customer,
                    thread=thread,
                    inbound_message_id=message.id,
                    inbound_text=message.body_text or "",
                    provider=autoreply_provider,
                    settings=_settings_for_autoreply(),
                    now=now,
                )
                logger.info(
                    "email ai auto-reply org=%s thread=%s outcome=%s",
                    organization_id,
                    thread.id,
                    outcome,
                )
                db.refresh(thread)
                if (
                    outcome == "sent"
                    or outcome.startswith("fallback_sent")
                    or outcome == "handoff_sent"
                ):
                    support_service.sync_conversation(
                        db,
                        organization_id=organization_id,
                        channel=SupportChannel.EMAIL,
                        channel_conversation_id=thread.id,
                        ai_state=AIHandoffState(thread.ai_state.value),
                        direction="outbound",
                        sender_type=SenderType.AI if outcome == "sent" else SenderType.SYSTEM,
                        event_type="ai_reply" if outcome == "sent" else "ai_handoff",
                        customer_id=customer.id,
                        now=now,
                    )
                    db.commit()
    _finish(db, event.id, WebhookEventStatus.PROCESSED)
    return "processed"


def _settings_for_autoreply():
    from app.core.config import get_settings

    return get_settings()


def _apply_status(db: Session, event: EmailWebhookEvent) -> bool:
    payload = event.payload
    email_id = str(payload.get("email_id") or "")
    message = db.scalar(
        select(EmailMessage)
        .where(
            EmailMessage.organization_id == event.organization_id,
            EmailMessage.provider_message_id == email_id,
        )
        .with_for_update()
    )
    if message is None:
        return False
    incoming = state_service.provider_status_to_normalized(str(payload.get("status") or ""))
    if incoming is None:
        return True
    if not state_service.should_apply(message.status, incoming):
        return True
    now = _utcnow()
    message.status = incoming
    if incoming == EmailMessageStatus.DELIVERED:
        message.delivered_at = now
    elif incoming == EmailMessageStatus.BOUNCED:
        message.bounced_at = now
        message.error_code = f"bounce_{(payload.get('bounce_type') or 'unknown').lower()}"
        _suppress_recipient(db, message, reason="bounce", now=now)
    elif incoming == EmailMessageStatus.COMPLAINT:
        message.complained_at = now
        _suppress_recipient(db, message, reason="complaint", now=now)
    elif incoming == EmailMessageStatus.UNSUBSCRIBED:
        message.unsubscribed_at = now
        _suppress_recipient(db, message, reason="unsubscribed", now=now)
    elif incoming == EmailMessageStatus.FAILED:
        message.failed_at = now
        message.error_code = message.error_code or "provider_reported_failure"
    return True


def _suppress_recipient(db: Session, message: EmailMessage, *, reason: str, now: datetime) -> None:
    from app.db.models import Customer, EmailThread
    from app.services.email import consent as consent_service
    from app.services.support import timeline as timeline_service

    thread_row = db.scalar(
        select(EmailThread).where(
            EmailThread.id == message.thread_id,
            EmailThread.organization_id == message.organization_id,
        )
    )
    if thread_row is None or thread_row.customer_id is None:
        return
    customer = db.scalar(
        select(Customer).where(
            Customer.id == thread_row.customer_id,
            Customer.organization_id == message.organization_id,
        )
    )
    if customer is None:
        return
    consent_service.suppress(customer, reason=reason, now=now)
    timeline_service.record_event(
        db,
        organization_id=message.organization_id,
        event_type="email_bounced" if reason == "bounce" else "email_unsubscribed",
        customer_id=customer.id,
        payload={"reason": reason},
        now=now,
    )


def process_event(session_factory, event_id: uuid.UUID, settings: Settings) -> str:
    """Run one stored webhook event. Returns ``skipped``, ``processed``, ``ignored``, ``retry`` or
    ``failed``."""
    with session_factory() as db:
        event = _claim(db, event_id)
        if event is None:
            return "skipped"
        if event.organization_id is None or event.connection_id is None:
            _finish(db, event.id, WebhookEventStatus.IGNORED, "unknown_connection")
            return "ignored"
        connection = db.get(EmailConnection, event.connection_id)
        if connection is None or connection.status == WhatsAppConnectionStatus.DISCONNECTED:
            _finish(db, event.id, WebhookEventStatus.IGNORED, "not_connected")
            return "ignored"
        try:
            if event.kind == "inbound":
                from app.services.email.factory import get_email_provider

                provider = get_email_provider()
                outcome = _process_inbound(db, event, connection, provider)
                if outcome == "retry":
                    db.commit()
                    _finish(db, event.id, WebhookEventStatus.PENDING)
                    return "retry"
                return outcome
            handled = _apply_status(db, event)
            if not handled:
                if event.attempts >= MAX_ATTEMPTS:
                    db.commit()
                    _finish(db, event.id, WebhookEventStatus.FAILED, "message_not_found")
                    return "failed"
                db.commit()
                _finish(db, event.id, WebhookEventStatus.PENDING)
                return "retry"
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("email webhook processing crashed event=%s", event_id)
            _finish(db, event.id, WebhookEventStatus.FAILED, "internal_error")
            return "failed"
        _finish(db, event.id, WebhookEventStatus.PROCESSED)
        return "processed"


# --- maintenance -------------------------------------------------------------------------------


def requeue_pending_events(session_factory, queue: JobQueue, older_than_seconds: int) -> int:
    cutoff = _utcnow() - timedelta(seconds=older_than_seconds)
    with session_factory() as db:
        db.execute(
            update(EmailWebhookEvent)
            .where(
                EmailWebhookEvent.status == WebhookEventStatus.PROCESSING,
                EmailWebhookEvent.received_at < cutoff,
            )
            .values(status=WebhookEventStatus.PENDING)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        ids = list(
            db.scalars(
                select(EmailWebhookEvent.id).where(
                    EmailWebhookEvent.status == WebhookEventStatus.PENDING,
                    EmailWebhookEvent.received_at < cutoff,
                )
            )
        )
    for event_id in ids:
        queue.enqueue(EmailWebhookJob(event_id))
    return len(ids)


def prune_old_events(session_factory, retention_days: int) -> int:
    cutoff = _utcnow() - timedelta(days=retention_days)
    with session_factory() as db:
        result = db.execute(
            delete(EmailWebhookEvent)
            .where(
                EmailWebhookEvent.received_at < cutoff,
                EmailWebhookEvent.status.in_(
                    [
                        WebhookEventStatus.PROCESSED,
                        WebhookEventStatus.IGNORED,
                        WebhookEventStatus.FAILED,
                    ]
                ),
            )
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
