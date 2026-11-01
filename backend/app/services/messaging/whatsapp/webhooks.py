"""WhatsApp webhooks: verification, intake and processing.

**Intake** (the HTTP request) does no real work: verify signature -> parse and bound the payload
-> resolve the connection by phone_number_id (never trust an organization id from the payload) ->
deduplicate -> store a ledger row -> enqueue -> answer 200 quickly.

**Processing** happens in the worker:

    message  -> normalize -> get/create contact -> open/refresh the conversation window ->
                store the inbound WhatsAppMessage -> emit an automation event -> (optionally) a
                minimal, policy-gated AI auto-reply when the organization has opted in
    status   -> look up the WhatsAppMessage by provider_message_id -> apply the state machine's
                rank-checked transition (idempotent; never regresses)

Unlike Shopify, there is no endpoint to re-fetch an inbound message's content after the fact, so
the ledger's ``payload`` column carries the already-parsed, already-bounded normalized fields
(never the raw Meta request body). See the ``WhatsAppWebhookEvent`` model docstring.
"""

import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.secrets import SecretManager
from app.db.models import (
    AIHandoffState,
    SenderType,
    SupportChannel,
    WebhookEventStatus,
    WhatsAppConnection,
    WhatsAppConnectionStatus,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppMessageDirection,
    WhatsAppMessageStatus,
    WhatsAppMessageType,
    WhatsAppWebhookEvent,
)
from app.services.messaging.phone import InvalidPhoneNumber, normalize_phone
from app.services.messaging.whatsapp import state as state_service
from app.services.messaging.whatsapp import window as window_service
from app.services.messaging.whatsapp.contacts import get_or_create_contact
from app.services.messaging.whatsapp.validation import verify_meta_signature
from app.workers.queue import JobQueue, WhatsAppWebhookJob

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
MAX_EVENTS_PER_REQUEST = 200


class WebhookRejected(Exception):
    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


@dataclass(frozen=True)
class IntakeResult:
    status: Literal["accepted", "duplicate", "ignored"]
    event_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ParsedInboundMessage:
    phone_number_id: str
    wamid: str
    from_wa_id: str
    message_type: str
    text_body: str | None
    timestamp: datetime
    contact_name: str | None


@dataclass(frozen=True)
class ParsedStatus:
    phone_number_id: str
    wamid: str
    status: str
    recipient_wa_id: str
    timestamp: datetime
    # Meta's own error code for a "failed" status (e.g. 131026 "recipient not a WhatsApp user"),
    # per https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes. An integer
    # code is safe to store and display -- it is never raw error text. None for any other status,
    # or if Meta did not include one.
    error_code: int | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _epoch(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


# --- verification --------------------------------------------------------------------------------


def verify_handshake(
    *, mode: str | None, token: str | None, challenge: str | None, configured_token: str
) -> str | None:
    """The plain-text body to answer with, or ``None`` if the handshake is invalid."""
    from app.services.messaging.whatsapp.validation import verify_handshake as _verify

    if challenge is None or not _verify(mode=mode, token=token, configured_token=configured_token):
        return None
    return challenge


# --- parsing -------------------------------------------------------------------------------------


def parse_webhook_payload(
    body: Mapping[str, Any],
) -> tuple[list[ParsedInboundMessage], list[ParsedStatus]]:
    """Walk ``entry[].changes[].value``, extracting only known, bounded, structurally valid
    fields. Anything malformed is silently skipped (fails closed per item, not for the request)."""
    messages: list[ParsedInboundMessage] = []
    statuses: list[ParsedStatus] = []
    if body.get("object") != "whatsapp_business_account":
        return messages, statuses
    for entry in body.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if len(messages) + len(statuses) > MAX_EVENTS_PER_REQUEST:
                return messages, statuses
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            phone_number_id = (
                metadata.get("phone_number_id") if isinstance(metadata, dict) else None
            )
            if not isinstance(phone_number_id, str) or not phone_number_id:
                continue
            names_by_wa_id: dict[str, str | None] = {}
            for contact in value.get("contacts") or []:
                if isinstance(contact, dict) and isinstance(contact.get("wa_id"), str):
                    profile = contact.get("profile")
                    name = profile.get("name") if isinstance(profile, dict) else None
                    names_by_wa_id[contact["wa_id"]] = name if isinstance(name, str) else None
            for msg in value.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                wamid, from_wa_id, msg_type = msg.get("id"), msg.get("from"), msg.get("type")
                timestamp = _epoch(msg.get("timestamp"))
                if not (
                    isinstance(wamid, str)
                    and wamid
                    and isinstance(from_wa_id, str)
                    and from_wa_id
                    and isinstance(msg_type, str)
                    and msg_type
                    and timestamp is not None
                ):
                    continue
                text_body = None
                if msg_type == "text":
                    text = msg.get("text")
                    if isinstance(text, dict) and isinstance(text.get("body"), str):
                        text_body = text["body"][:4096]
                messages.append(
                    ParsedInboundMessage(
                        phone_number_id=phone_number_id,
                        wamid=wamid,
                        from_wa_id=from_wa_id,
                        message_type=msg_type,
                        text_body=text_body,
                        timestamp=timestamp,
                        contact_name=names_by_wa_id.get(from_wa_id),
                    )
                )
            for st in value.get("statuses") or []:
                if not isinstance(st, dict):
                    continue
                wamid, status, recipient = st.get("id"), st.get("status"), st.get("recipient_id")
                timestamp = _epoch(st.get("timestamp"))
                if not (
                    isinstance(wamid, str)
                    and wamid
                    and isinstance(status, str)
                    and status
                    and isinstance(recipient, str)
                    and recipient
                    and timestamp is not None
                ):
                    continue
                error_code = None
                if status == "failed":
                    errors = st.get("errors")
                    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                        code = errors[0].get("code")
                        error_code = code if isinstance(code, int) else None
                statuses.append(
                    ParsedStatus(
                        phone_number_id=phone_number_id,
                        wamid=wamid,
                        status=status,
                        recipient_wa_id=recipient,
                        timestamp=timestamp,
                        error_code=error_code,
                    )
                )
    return messages, statuses


# --- intake ---------------------------------------------------------------------------------------


def _connection_for_phone(db: Session, phone_number_id: str) -> WhatsAppConnection | None:
    return db.scalar(
        select(WhatsAppConnection).where(
            WhatsAppConnection.phone_number_id == phone_number_id,
            WhatsAppConnection.status != WhatsAppConnectionStatus.DISCONNECTED,
        )
    )


def _ledger(
    db: Session,
    queue: JobQueue,
    *,
    phone_number_id: str,
    dedupe_key: str,
    kind: str,
    resource_wamid: str,
    payload: dict[str, Any],
    now: datetime,
) -> IntakeResult:
    connection = _connection_for_phone(db, phone_number_id)
    if connection is None:
        return IntakeResult("ignored")
    event = WhatsAppWebhookEvent(
        id=uuid.uuid4(),
        phone_number_id=phone_number_id,
        dedupe_key=dedupe_key,
        kind=kind,
        organization_id=connection.organization_id,
        connection_id=connection.id,
        resource_wamid=resource_wamid,
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
        queue.enqueue(WhatsAppWebhookJob(event.id))
    except Exception:
        logger.exception("could not enqueue whatsapp webhook event=%s", event.id)
        db.execute(delete(WhatsAppWebhookEvent).where(WhatsAppWebhookEvent.id == event.id))
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
) -> list[IntakeResult]:
    """Handle one delivery. ``headers`` must have lower-cased names. Raises ``WebhookRejected``."""
    now = now or _utcnow()
    secret = settings.whatsapp_app_secret.get_secret_value() if settings.whatsapp_app_secret else ""
    if not verify_meta_signature(body, headers.get("x-hub-signature-256"), secret):
        raise WebhookRejected(401, "invalid signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise WebhookRejected(400, "body is not JSON") from None
    if not isinstance(payload, dict):
        raise WebhookRejected(400, "body is not an object")

    messages, statuses = parse_webhook_payload(payload)
    if len(messages) + len(statuses) > MAX_EVENTS_PER_REQUEST:
        raise WebhookRejected(400, "too many events in one delivery")

    results: list[IntakeResult] = []
    for msg in messages:
        results.append(
            _ledger(
                db,
                queue,
                phone_number_id=msg.phone_number_id,
                dedupe_key=f"msg:{msg.wamid}",
                kind="message",
                resource_wamid=msg.wamid,
                payload={
                    "wamid": msg.wamid,
                    "from_wa_id": msg.from_wa_id,
                    "message_type": msg.message_type,
                    "text_body": msg.text_body,
                    "timestamp": msg.timestamp.isoformat(),
                    "contact_name": msg.contact_name,
                },
                now=now,
            )
        )
    for st in statuses:
        results.append(
            _ledger(
                db,
                queue,
                phone_number_id=st.phone_number_id,
                dedupe_key=f"status:{st.wamid}:{st.status}:{int(st.timestamp.timestamp())}",
                kind="status",
                resource_wamid=st.wamid,
                payload={
                    "wamid": st.wamid,
                    "status": st.status,
                    "recipient_wa_id": st.recipient_wa_id,
                    "timestamp": st.timestamp.isoformat(),
                    "error_code": st.error_code,
                },
                now=now,
            )
        )
    return results


# --- processing --------------------------------------------------------------------------------


def _claim(db: Session, event_id: uuid.UUID) -> WhatsAppWebhookEvent | None:
    result = db.execute(
        update(WhatsAppWebhookEvent)
        .where(
            WhatsAppWebhookEvent.id == event_id,
            WhatsAppWebhookEvent.status == WebhookEventStatus.PENDING,
        )
        .values(status=WebhookEventStatus.PROCESSING, attempts=WhatsAppWebhookEvent.attempts + 1)
    )
    db.commit()
    if result.rowcount != 1:  # type: ignore[attr-defined]
        return None
    return db.get(WhatsAppWebhookEvent, event_id)


def _finish(
    db: Session, event_id: uuid.UUID, status: WebhookEventStatus, error_code: str | None = None
) -> None:
    db.execute(
        update(WhatsAppWebhookEvent)
        .where(WhatsAppWebhookEvent.id == event_id)
        .values(
            status=status,
            error_code=error_code,
            processed_at=_utcnow() if status != WebhookEventStatus.PENDING else None,
        )
    )
    db.commit()


def _process_message(
    db: Session,
    event: WhatsAppWebhookEvent,
    connection: WhatsAppConnection,
    settings: Settings,
    secrets: SecretManager | None,
) -> str | None:
    # The caller (process_event) already refused to reach here unless both are set.
    assert event.organization_id is not None
    organization_id = event.organization_id
    payload = event.payload
    from_wa_id = str(payload.get("from_wa_id") or "")
    try:
        phone_e164 = normalize_phone(f"+{from_wa_id}")
    except InvalidPhoneNumber:
        _finish(db, event.id, WebhookEventStatus.IGNORED, "invalid_sender_phone")
        return "ignored"
    now = _utcnow()
    contact = get_or_create_contact(
        db,
        organization_id=organization_id,
        phone_e164=phone_e164,
        wa_id=from_wa_id,
        display_name=payload.get("contact_name"),
    )
    conversation = window_service.get_or_create_conversation(
        db,
        organization_id=organization_id,
        contact_id=contact.id,
        connection_id=connection.id,
    )
    is_first_message = conversation.message_count == 0
    window_service.record_inbound(
        conversation, now=now, window_hours=settings.whatsapp_customer_service_window_hours
    )
    message_type = str(payload.get("message_type") or "")
    text_body = payload.get("text_body")
    message = WhatsAppMessage(
        id=uuid.uuid4(),
        organization_id=event.organization_id,
        conversation_id=conversation.id,
        direction=WhatsAppMessageDirection.INBOUND,
        message_type=(
            WhatsAppMessageType.TEXT if message_type == "text" else WhatsAppMessageType.MEDIA
        ),
        status=WhatsAppMessageStatus.RECEIVED,
        idempotency_key=f"inbound:{event.resource_wamid}",
        provider_message_id=event.resource_wamid,
        content=text_body if isinstance(text_body, str) else None,
        sender_type=SenderType.CUSTOMER,
    )
    is_inbound_retry = False
    db.add(message)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # The same inbound message was already stored in a prior crashed attempt or
        # duplicate delivery
        existing_inbound = db.scalar(
            select(WhatsAppMessage).where(
                WhatsAppMessage.organization_id == organization_id,
                WhatsAppMessage.idempotency_key == f"inbound:{event.resource_wamid}",
            )
        )
        if existing_inbound is None:
            _finish(db, event.id, WebhookEventStatus.PROCESSED)
            return "processed"
        message = existing_inbound
        existing_conv = db.get(WhatsAppConversation, message.conversation_id)
        if existing_conv is not None:
            conversation = existing_conv
            window_service.record_inbound(
                conversation, now=now, window_hours=settings.whatsapp_customer_service_window_hours
            )
            db.commit()
        is_inbound_retry = True

    from app.services.support import identity as identity_service

    customer = identity_service.get_or_create_customer(
        db,
        organization_id=organization_id,
        phone=contact.phone_e164,
        name=contact.display_name,
        default_country=connection.default_country,
    )
    db.commit()

    if not is_inbound_retry:
        from app.services.automation import ingest_event
        from app.workers.queue import get_job_queue

        event_type = (
            "whatsapp_conversation_started" if is_first_message else "whatsapp_message_received"
        )
        ingest_event(
            db,
            get_job_queue(),
            organization_id=organization_id,
            event_type=event_type,
            source="whatsapp",
            source_id=event.resource_wamid or "",
            dedupe_key=f"whatsapp:message:{event.resource_wamid}",
            payload={
                "event": {"source": "whatsapp"},
                "customer": {"phone": contact.phone_e164, "exists": True, "id": str(customer.id)},
                "whatsapp": {
                    "conversation_id": str(conversation.id),
                    "contact_id": str(contact.id),
                    "message_id": str(message.id),
                    "text": (text_body if isinstance(text_body, str) else "")[:500],
                },
            },
        )
        from app.services.support import conversations as support_service

        support_service.sync_conversation(
            db,
            organization_id=organization_id,
            channel=SupportChannel.WHATSAPP,
            channel_conversation_id=conversation.id,
            ai_state=AIHandoffState(conversation.ai_state.value),
            direction="inbound",
            sender_type=SenderType.CUSTOMER,
            event_type="whatsapp_inbound",
            customer_id=customer.id,
            message_preview=(text_body if isinstance(text_body, str) else "")[:300],
        )
        db.commit()

    effective_text = message.content or (text_body if isinstance(text_body, str) else "")
    if connection.ai_auto_reply_enabled and effective_text.strip():
        if event.attempts > MAX_ATTEMPTS:
            _finish(db, event.id, WebhookEventStatus.FAILED, "max_attempts_exceeded")
            return "failed"

        from app.services.messaging.factory import get_messaging_provider
        from app.services.messaging.whatsapp import autoreply
        from app.services.messaging.whatsapp.send import decrypt_access_token

        try:
            provider = get_messaging_provider()
            access_token = (
                decrypt_access_token(connection, secrets) if secrets is not None else None
            )
        except Exception:
            access_token = None
            provider = None
        if provider is not None and access_token is not None:
            outcome = autoreply.maybe_auto_reply(
                db,
                organization_id=organization_id,
                connection=connection,
                contact=contact,
                conversation=conversation,
                inbound_message_id=message.id,
                inbound_text=effective_text,
                inbound_wamid=event.resource_wamid or "",
                provider=provider,
                access_token=access_token,
                settings=settings,
                now=now,
            )
            logger.info(
                "whatsapp ai auto-reply org=%s conversation=%s outcome=%s",
                event.organization_id,
                conversation.id,
                outcome,
            )
            if (
                outcome == "sent"
                or outcome.startswith("fallback_sent")
                or outcome == "handoff_sent"
            ):
                from app.services.support import conversations as support_service

                db.refresh(conversation)
                support_service.sync_conversation(
                    db,
                    organization_id=organization_id,
                    channel=SupportChannel.WHATSAPP,
                    channel_conversation_id=conversation.id,
                    ai_state=AIHandoffState(conversation.ai_state.value),
                    direction="outbound",
                    sender_type=SenderType.AI if outcome == "sent" else SenderType.SYSTEM,
                    event_type="ai_reply" if outcome == "sent" else "ai_handoff",
                    customer_id=customer.id,
                    now=now,
                )
                db.commit()
    return None


def _apply_status(db: Session, event: WhatsAppWebhookEvent) -> bool:
    """Returns True if handled (processed, even if a no-op); False means "retry later" (the
    referenced message has not been committed yet -- see the module docstring)."""
    payload = event.payload
    wamid = str(payload.get("wamid") or "")
    message = db.scalar(
        select(WhatsAppMessage)
        .where(
            WhatsAppMessage.organization_id == event.organization_id,
            WhatsAppMessage.provider_message_id == wamid,
        )
        .with_for_update()
    )
    if message is None:
        return False
    incoming = state_service.provider_status_to_normalized(str(payload.get("status") or ""))
    if incoming is None:
        return True  # an unrecognized status value: informational only, nothing to apply
    if not state_service.should_apply(message.status, incoming):
        return True  # duplicate or out-of-order: a safe no-op, not an error
    now = _utcnow()
    message.status = incoming
    if incoming == WhatsAppMessageStatus.DELIVERED:
        message.delivered_at = now
    elif incoming == WhatsAppMessageStatus.READ:
        message.read_at = now
    elif incoming == WhatsAppMessageStatus.FAILED:
        message.failed_at = now
        reported_code = payload.get("error_code")
        message.error_code = message.error_code or (
            f"meta_{reported_code}"
            if isinstance(reported_code, int)
            else "provider_reported_failure"
        )
    return True


def process_event(
    session_factory, event_id: uuid.UUID, settings: Settings, secrets: SecretManager | None = None
) -> str:
    """Run one stored webhook event. Returns ``skipped``, ``processed``, ``ignored``, ``retry`` or
    ``failed``."""
    with session_factory() as db:
        event = _claim(db, event_id)
        if event is None:
            return "skipped"
        if event.organization_id is None or event.connection_id is None:
            _finish(db, event.id, WebhookEventStatus.IGNORED, "unknown_connection")
            return "ignored"
        connection = db.get(WhatsAppConnection, event.connection_id)
        if connection is None or connection.status == WhatsAppConnectionStatus.DISCONNECTED:
            _finish(db, event.id, WebhookEventStatus.IGNORED, "not_connected")
            return "ignored"
        try:
            if event.kind == "message":
                outcome = _process_message(db, event, connection, settings, secrets)
                if outcome is not None:
                    return outcome
            else:
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
            logger.exception("whatsapp webhook processing crashed event=%s", event_id)
            _finish(db, event.id, WebhookEventStatus.FAILED, "internal_error")
            return "failed"
        _finish(db, event.id, WebhookEventStatus.PROCESSED)
        return "processed"


# --- maintenance -------------------------------------------------------------------------------


def requeue_pending_events(session_factory, queue: JobQueue, older_than_seconds: int) -> int:
    cutoff = _utcnow() - timedelta(seconds=older_than_seconds)
    with session_factory() as db:
        db.execute(
            update(WhatsAppWebhookEvent)
            .where(
                WhatsAppWebhookEvent.status == WebhookEventStatus.PROCESSING,
                WhatsAppWebhookEvent.received_at < cutoff,
            )
            .values(status=WebhookEventStatus.PENDING)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        ids = list(
            db.scalars(
                select(WhatsAppWebhookEvent.id).where(
                    WhatsAppWebhookEvent.status == WebhookEventStatus.PENDING,
                    WhatsAppWebhookEvent.received_at < cutoff,
                )
            )
        )
    for event_id in ids:
        queue.enqueue(WhatsAppWebhookJob(event_id))
    return len(ids)


def prune_old_events(session_factory, retention_days: int) -> int:
    cutoff = _utcnow() - timedelta(days=retention_days)
    with session_factory() as db:
        result = db.execute(
            delete(WhatsAppWebhookEvent)
            .where(
                WhatsAppWebhookEvent.received_at < cutoff,
                WhatsAppWebhookEvent.status.in_(
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
