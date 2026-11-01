"""WhatsApp outbound send orchestration: the policy gate, the idempotent message row, and the
provider call. This is the only module that may call a ``MessagingProvider.send_*`` method for
WhatsApp; nothing upstream (automation, AI) talks to a provider directly.

Policy gate order (fixed; every check runs before any provider call, and a failure here never
reaches the provider):

    1. the organization has not exceeded its configured send rate limit (WHATSAPP_SEND_RATE_LIMIT
       per WHATSAPP_SEND_RATE_WINDOW_SECONDS) -- protects Meta's own per-number throughput tier
       from a runaway workflow/retry loop; classified RATE_LIMITED, same as a provider-reported
       throughput error, so the existing retry/backoff path handles both identically
    2. the connection is CONNECTED
    3. the contact is not globally suppressed
    4. consent for the message's category is OPTED_IN (a missing row, UNKNOWN, is never treated
       as opted in)
    5. a free-form text requires the customer-service window to be open; a template requires
       APPROVED status instead (and may be sent in or out of the window)
    6. content/template variables validate

AI never participates in any of this: it only ever supplies candidate text, which arrives here as
plain ``content`` -- a string like any other, already generated and already past the automation
engine's own output sanitation (see ``app/services/automation.py``).

Idempotency: a ``WhatsAppMessage`` row is looked up by ``(organization_id, idempotency_key)``
before anything else. A row found in any status other than QUEUED is returned as-is and the
provider is never called again -- this is what makes a duplicate automation retry (or a retry
whose previous attempt crashed after the provider call) safe. A row in QUEUED is atomically
claimed (``UPDATE ... WHERE status = 'QUEUED'``) immediately before the provider call; on a
*confirmed* retryable provider failure (never on a crash -- a crash leaves the row SENDING,
deliberately not retried automatically) the row is put back to QUEUED so the next retry can claim
it again. SENDING is therefore only ever a few-hundred-millisecond in-flight marker during one
synchronous call; if a worker dies inside it, the row stays SENDING and is never auto-resent --
the automation engine's own crash recovery fails that execution outright (see
``recover_due`` / ``worker_lost_during_whatsapp_send`` in ``app/services/automation.py``), exactly
mirroring how an in-flight AI call is already handled. A cosmetic janitor
(``cancel_orphaned_sending``) later marks such rows FAILED for dashboard hygiene; it never retries.
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.core.secrets import SecretError, SecretManager
from app.db.models import (
    SenderType,
    WhatsAppConnection,
    WhatsAppConnectionStatus,
    WhatsAppConsentCategory,
    WhatsAppConsentStatus,
    WhatsAppContact,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppMessageDirection,
    WhatsAppMessageStatus,
    WhatsAppMessageType,
)
from app.services.messaging.errors import MessagingError, MessagingErrorCode
from app.services.messaging.provider import MessagingProvider, SendRequest, SendResult
from app.services.messaging.whatsapp import consent as consent_service
from app.services.messaging.whatsapp import templates as template_service
from app.services.messaging.whatsapp import window as window_service

SECRET_CONTEXT = "whatsapp:access_token:{connection_id}"
MAX_TEXT_LENGTH = 4096  # Meta's documented text body limit; not reverified live this session
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
GENERATED_TEXT_TOKEN = "{{generated_text}}"


@dataclass(frozen=True)
class OutboundText:
    content: str
    consent_category: WhatsAppConsentCategory


@dataclass(frozen=True)
class OutboundTemplate:
    template_name: str
    template_language: str
    variables: dict[str, str] = field(default_factory=dict)
    consent_category: WhatsAppConsentCategory = WhatsAppConsentCategory.MARKETING


def sanitize_text(raw: str) -> str:
    cleaned = _CONTROL_CHARS.sub("", raw).strip()
    return cleaned[:MAX_TEXT_LENGTH]


def decrypt_access_token(connection: WhatsAppConnection, secrets: SecretManager) -> str:
    if not connection.access_token_encrypted:
        raise MessagingError(MessagingErrorCode.UNCONFIGURED, "no WhatsApp access token stored")
    try:
        return secrets.decrypt(
            connection.access_token_encrypted,
            context=SECRET_CONTEXT.format(connection_id=connection.id),
        )
    except SecretError as exc:
        raise MessagingError(
            MessagingErrorCode.UNCONFIGURED, f"WhatsApp token unusable ({exc.code})"
        ) from None


def _existing(
    db: Session, organization_id: uuid.UUID, idempotency_key: str
) -> WhatsAppMessage | None:
    return db.scalar(
        select(WhatsAppMessage).where(
            WhatsAppMessage.organization_id == organization_id,
            WhatsAppMessage.idempotency_key == idempotency_key,
        )
    )


def _rate_limit_exceeded(organization_id: uuid.UUID) -> bool:
    """One send-side token bucket per organization (WHATSAPP_SEND_RATE_LIMIT per
    WHATSAPP_SEND_RATE_WINDOW_SECONDS), so a misconfigured or looping workflow cannot generate
    unbounded outbound traffic against Meta's own per-number throughput tier. Shared with the
    rest of the app's rate limiting (Redis-backed across workers when configured, falling back to
    an in-process limiter if Redis is unavailable -- see ``app.core.rate_limit``)."""
    settings = get_settings()
    return not limiter.allow(
        f"whatsapp-send:{organization_id}",
        settings.whatsapp_send_rate_limit,
        settings.whatsapp_send_rate_window_seconds,
    )


def _base_policy_block(
    db: Session,
    *,
    organization_id: uuid.UUID,
    contact_id: uuid.UUID,
    connection: WhatsAppConnection,
    category: WhatsAppConsentCategory,
) -> MessagingErrorCode | None:
    if _rate_limit_exceeded(organization_id):
        return MessagingErrorCode.RATE_LIMITED
    if connection.status != WhatsAppConnectionStatus.CONNECTED:
        return MessagingErrorCode.UNCONFIGURED
    if consent_service.is_suppressed(db, organization_id=organization_id, contact_id=contact_id):
        return MessagingErrorCode.POLICY_BLOCKED
    status = consent_service.get_consent_status(
        db, organization_id=organization_id, contact_id=contact_id, category=category
    )
    if status != WhatsAppConsentStatus.OPTED_IN:
        return MessagingErrorCode.OPTED_OUT
    return None


def _write_failed(
    db: Session,
    row: WhatsAppMessage | None,
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    direction: WhatsAppMessageDirection,
    message_type: WhatsAppMessageType,
    idempotency_key: str,
    content: str | None,
    template_name: str | None,
    template_language: str | None,
    safe_metadata: dict | None,
    code: MessagingErrorCode,
    now: datetime,
    sender_type: SenderType | None = None,
    sender_user_id: uuid.UUID | None = None,
) -> WhatsAppMessage:
    if row is None:
        row = WhatsAppMessage(
            id=uuid.uuid4(),
            organization_id=organization_id,
            conversation_id=conversation_id,
            direction=direction,
            message_type=message_type,
            status=WhatsAppMessageStatus.QUEUED,
            idempotency_key=idempotency_key,
            content=content,
            template_name=template_name,
            template_language=template_language,
            safe_metadata=safe_metadata,
            sender_type=sender_type,
            sender_user_id=sender_user_id,
        )
        db.add(row)
    row.status = WhatsAppMessageStatus.FAILED
    row.error_code = code.value
    row.failed_at = now
    db.commit()
    return row


def _claim_queued(db: Session, row: WhatsAppMessage) -> bool:
    result = db.execute(
        update(WhatsAppMessage)
        .where(WhatsAppMessage.id == row.id, WhatsAppMessage.status == WhatsAppMessageStatus.QUEUED)
        .values(status=WhatsAppMessageStatus.SENDING)
    )
    db.commit()
    return result.rowcount == 1  # type: ignore[attr-defined]


def _dispatch(
    db: Session,
    *,
    row: WhatsAppMessage,
    conversation: WhatsAppConversation,
    provider: MessagingProvider,
    send_fn: Callable[[], SendResult],
    now: datetime,
) -> WhatsAppMessage:
    try:
        result = send_fn()
    except MessagingError as exc:
        if exc.retryable:
            db.execute(
                update(WhatsAppMessage)
                .where(
                    WhatsAppMessage.id == row.id,
                    WhatsAppMessage.status == WhatsAppMessageStatus.SENDING,
                )
                .values(status=WhatsAppMessageStatus.QUEUED)
            )
        else:
            db.execute(
                update(WhatsAppMessage)
                .where(WhatsAppMessage.id == row.id)
                .values(
                    status=WhatsAppMessageStatus.FAILED, error_code=exc.code.value, failed_at=now
                )
            )
        db.commit()
        raise
    db.execute(
        update(WhatsAppMessage)
        .where(WhatsAppMessage.id == row.id)
        .values(
            status=WhatsAppMessageStatus.SENT,
            provider_message_id=result.provider_message_id,
            sent_at=now,
        )
    )
    window_service.record_outbound(conversation, now=now)
    db.commit()
    db.refresh(row)
    return row


def send_text(
    db: Session,
    *,
    organization_id: uuid.UUID,
    connection: WhatsAppConnection,
    contact: WhatsAppContact,
    outbound: OutboundText,
    idempotency_key: str,
    provider: MessagingProvider,
    access_token: str,
    safe_metadata: dict | None = None,
    now: datetime | None = None,
    sender_type: SenderType | None = None,
    sender_user_id: uuid.UUID | None = None,
) -> WhatsAppMessage:
    now = now or datetime.now(UTC)
    existing = _existing(db, organization_id, idempotency_key)
    if existing is not None and existing.status != WhatsAppMessageStatus.QUEUED:
        return existing

    conversation = window_service.get_or_create_conversation(
        db, organization_id=organization_id, contact_id=contact.id, connection_id=connection.id
    )
    content = sanitize_text(outbound.content)

    code = _base_policy_block(
        db,
        organization_id=organization_id,
        contact_id=contact.id,
        connection=connection,
        category=outbound.consent_category,
    )
    if code is None and not window_service.is_window_open(conversation, now=now):
        code = MessagingErrorCode.WINDOW_CLOSED
    if code is None and not content:
        code = MessagingErrorCode.INVALID_REQUEST
    if code is not None:
        return _write_failed(
            db,
            existing,
            organization_id=organization_id,
            conversation_id=conversation.id,
            direction=WhatsAppMessageDirection.OUTBOUND,
            message_type=WhatsAppMessageType.TEXT,
            idempotency_key=idempotency_key,
            content=content,
            template_name=None,
            template_language=None,
            safe_metadata=safe_metadata,
            code=code,
            now=now,
            sender_type=sender_type,
            sender_user_id=sender_user_id,
        )

    row = existing
    if row is None:
        row = WhatsAppMessage(
            id=uuid.uuid4(),
            organization_id=organization_id,
            conversation_id=conversation.id,
            direction=WhatsAppMessageDirection.OUTBOUND,
            message_type=WhatsAppMessageType.TEXT,
            status=WhatsAppMessageStatus.QUEUED,
            idempotency_key=idempotency_key,
            content=content,
            safe_metadata=safe_metadata,
            sender_type=sender_type,
            sender_user_id=sender_user_id,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = _existing(db, organization_id, idempotency_key)
            if row is None:
                raise
            if row.status != WhatsAppMessageStatus.QUEUED:
                return row

    if not _claim_queued(db, row):
        db.refresh(row)
        return row

    request = SendRequest(
        to_e164=contact.phone_e164,
        kind="text",
        idempotency_key=idempotency_key,
        text=content,
        phone_number_id=connection.phone_number_id,
        access_token=access_token,
        api_version=connection.api_version,
    )
    return _dispatch(
        db,
        row=row,
        conversation=conversation,
        provider=provider,
        send_fn=lambda: provider.send_text(request),
        now=now,
    )


def send_template(
    db: Session,
    *,
    organization_id: uuid.UUID,
    connection: WhatsAppConnection,
    contact: WhatsAppContact,
    outbound: OutboundTemplate,
    idempotency_key: str,
    provider: MessagingProvider,
    access_token: str,
    safe_metadata: dict | None = None,
    now: datetime | None = None,
    sender_type: SenderType | None = None,
    sender_user_id: uuid.UUID | None = None,
) -> WhatsAppMessage:
    now = now or datetime.now(UTC)
    existing = _existing(db, organization_id, idempotency_key)
    if existing is not None and existing.status != WhatsAppMessageStatus.QUEUED:
        return existing

    conversation = window_service.get_or_create_conversation(
        db, organization_id=organization_id, contact_id=contact.id, connection_id=connection.id
    )

    code = _base_policy_block(
        db,
        organization_id=organization_id,
        contact_id=contact.id,
        connection=connection,
        category=outbound.consent_category,
    )
    template = template_service.get_template(
        db,
        organization_id=organization_id,
        connection_id=connection.id,
        name=outbound.template_name,
        language=outbound.template_language,
    )
    body_component = None
    if code is None:
        if template is None or not template_service.is_usable(template):
            code = MessagingErrorCode.TEMPLATE_NOT_APPROVED
        else:
            try:
                body_component = template_service.build_body_component(template, outbound.variables)
            except MessagingError as exc:
                code = exc.code
    if code is not None:
        return _write_failed(
            db,
            existing,
            organization_id=organization_id,
            conversation_id=conversation.id,
            direction=WhatsAppMessageDirection.OUTBOUND,
            message_type=WhatsAppMessageType.TEMPLATE,
            idempotency_key=idempotency_key,
            content=None,
            template_name=outbound.template_name,
            template_language=outbound.template_language,
            safe_metadata=safe_metadata,
            code=code,
            now=now,
            sender_type=sender_type,
            sender_user_id=sender_user_id,
        )

    row = existing
    if row is None:
        row = WhatsAppMessage(
            id=uuid.uuid4(),
            organization_id=organization_id,
            conversation_id=conversation.id,
            direction=WhatsAppMessageDirection.OUTBOUND,
            message_type=WhatsAppMessageType.TEMPLATE,
            status=WhatsAppMessageStatus.QUEUED,
            idempotency_key=idempotency_key,
            template_name=outbound.template_name,
            template_language=outbound.template_language,
            safe_metadata=safe_metadata,
            sender_type=sender_type,
            sender_user_id=sender_user_id,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = _existing(db, organization_id, idempotency_key)
            if row is None:
                raise
            if row.status != WhatsAppMessageStatus.QUEUED:
                return row

    if not _claim_queued(db, row):
        db.refresh(row)
        return row

    request = SendRequest(
        to_e164=contact.phone_e164,
        kind="template",
        idempotency_key=idempotency_key,
        template_name=outbound.template_name,
        template_language=outbound.template_language,
        template_components=(body_component,) if body_component else (),
        phone_number_id=connection.phone_number_id,
        access_token=access_token,
        api_version=connection.api_version,
    )
    return _dispatch(
        db,
        row=row,
        conversation=conversation,
        provider=provider,
        send_fn=lambda: provider.send_template(request),
        now=now,
    )


def cancel_orphaned_sending(
    db: Session, *, stale_after_seconds: int = 120, limit: int = 100
) -> int:
    """Dashboard hygiene only: a row stuck SENDING with no resolution is marked FAILED after a
    generous staleness window. This never triggers a retry or a second provider call -- the
    automation engine's own execution-level crash recovery is what actually prevents a duplicate
    send (see the module docstring)."""
    cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
    ids = list(
        db.scalars(
            select(WhatsAppMessage.id)
            .where(
                WhatsAppMessage.status == WhatsAppMessageStatus.SENDING,
                WhatsAppMessage.created_at < cutoff,
            )
            .limit(limit)
        )
    )
    if not ids:
        return 0
    db.execute(
        update(WhatsAppMessage)
        .where(WhatsAppMessage.id.in_(ids))
        .values(
            status=WhatsAppMessageStatus.FAILED,
            error_code="worker_lost",
            failed_at=datetime.now(UTC),
        )
    )
    db.commit()
    return len(ids)


def resolve_generated_text(content: str, generated_text: str | None) -> str:
    """The one bounded placeholder an action's text field may use: the exact literal
    ``{{generated_text}}`` (a whole-field match, never embedded in a larger string) is replaced
    with the immediately preceding ``ai_generate`` step's output in this execution. This is
    deliberately not a general templating language -- see docs/phase-6-progress.md."""
    if content == GENERATED_TEXT_TOKEN:
        return generated_text or ""
    return content
