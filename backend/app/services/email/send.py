"""Email outbound send orchestration: the policy gate, the idempotent message row, and the
provider call (mirrors ``app/services/messaging/whatsapp/send.py``). This is the only module that
may call ``EmailProvider.send``; nothing upstream (automation, AI, campaigns) talks to a provider
directly.

Policy gate order (fixed; every check runs before any provider call):

    1. the organization has not exceeded its configured send rate limit (EMAIL_SEND_RATE_LIMIT
       per EMAIL_SEND_RATE_WINDOW_SECONDS) -- protects against a runaway workflow/campaign loop
    2. the connection is CONNECTED and enabled
    3. the customer is not suppressed (bounce/complaint/unsubscribe) -- a universal kill switch,
       checked regardless of message classification, exactly like WhatsApp's suppression
    4. a MARKETING message additionally requires marketing consent (OPTED_IN); TRANSACTIONAL and
       LIFECYCLE email are never gated by marketing consent (brief section 20)

Idempotency: an ``EmailMessage`` row is looked up by ``(organization_id, idempotency_key)`` before
anything else. A row found in any status other than QUEUED is returned as-is and the provider is
never called again. A row in QUEUED is atomically claimed (``UPDATE ... WHERE status = 'QUEUED'``)
immediately before the provider call; on a confirmed retryable provider failure the row is put
back to QUEUED. A crash leaves the row SENDING, deliberately not auto-retried (mirrors WhatsApp).
"""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.db.models import (
    Customer,
    EmailClassification,
    EmailConnection,
    EmailDirection,
    EmailMessage,
    EmailMessageStatus,
    EmailThread,
    SenderType,
    WhatsAppConnectionStatus,
)
from app.services.email import consent as consent_service
from app.services.email import unsubscribe as unsubscribe_service
from app.services.email.errors import EmailError, EmailErrorCode
from app.services.email.provider import EmailProvider, SendRequest, SendResult

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Header injection: a raw '\r' or '\n' in a header-bound field (subject, addresses) could inject
# extra SMTP/MIME headers. Stripped unconditionally, never just validated-and-rejected, since a
# subject is otherwise free text.
_HEADER_INJECTION = re.compile(r"[\r\n]")
MAX_SUBJECT_LENGTH = 998


@dataclass(frozen=True)
class OutboundEmail:
    subject: str
    text_body: str
    html_body: str
    classification: EmailClassification
    in_reply_to_header: str | None = None
    references_header: str | None = None
    template_id: uuid.UUID | None = None
    campaign_id: uuid.UUID | None = None


def sanitize_header(raw: str, *, max_len: int) -> str:
    return _HEADER_INJECTION.sub(" ", _CONTROL_CHARS.sub("", raw)).strip()[:max_len]


def _rate_limit_exceeded(organization_id: uuid.UUID) -> bool:
    settings = get_settings()
    return not limiter.allow(
        f"email-send:{organization_id}",
        settings.email_send_rate_limit,
        settings.email_send_rate_window_seconds,
    )


def _existing(db: Session, organization_id: uuid.UUID, idempotency_key: str) -> EmailMessage | None:
    return db.scalar(
        select(EmailMessage).where(
            EmailMessage.organization_id == organization_id,
            EmailMessage.idempotency_key == idempotency_key,
        )
    )


def _base_policy_block(
    *, connection: EmailConnection, customer: Customer, classification: EmailClassification
) -> EmailErrorCode | None:
    if _rate_limit_exceeded(connection.organization_id):
        return EmailErrorCode.RATE_LIMITED
    if connection.status != WhatsAppConnectionStatus.CONNECTED or not connection.enabled:
        return EmailErrorCode.UNCONFIGURED
    if consent_service.is_suppressed(customer):
        return EmailErrorCode.SUPPRESSED
    if (
        classification == EmailClassification.MARKETING
        and customer.email_marketing_consent is not True
    ):
        return EmailErrorCode.OPTED_OUT
    if classification == EmailClassification.MARKETING and not _unsubscribe_link_configured():
        return EmailErrorCode.POLICY_BLOCKED
    return None


def _unsubscribe_link_configured() -> bool:
    settings = get_settings()
    return bool(settings.email_unsubscribe_base_url) and settings.secret_encryption_key is not None


def _with_unsubscribe_footer(
    outbound: OutboundEmail, *, organization_id: uuid.UUID, customer_id: uuid.UUID
) -> tuple[str, str]:
    """Returns ``(text_body, html_body)``, with a one-click unsubscribe link appended for a
    MARKETING send. Every other classification is returned unchanged -- transactional and
    lifecycle email are never gated or decorated by this (brief section 20)."""
    if outbound.classification != EmailClassification.MARKETING:
        return outbound.text_body, outbound.html_body
    settings = get_settings()
    # Guaranteed by the `_base_policy_block` gate every caller passes through first for a
    # MARKETING send -- never reachable with no key configured.
    assert settings.secret_encryption_key is not None
    key = unsubscribe_service.derive_key(settings.secret_encryption_key.get_secret_value())
    url = unsubscribe_service.build_unsubscribe_url(
        base_url=settings.email_unsubscribe_base_url,
        organization_id=organization_id,
        customer_id=customer_id,
        key=key,
    )
    text_body = f"{outbound.text_body}\n\nUnsubscribe: {url}"
    html_body = (
        f'{outbound.html_body}<p><a href="{url}">Unsubscribe</a></p>' if outbound.html_body else ""
    )
    return text_body, html_body


def _write_failed(
    db: Session,
    row: EmailMessage | None,
    *,
    organization_id: uuid.UUID,
    thread_id: uuid.UUID,
    idempotency_key: str,
    from_address: str,
    to_address: str,
    outbound: OutboundEmail,
    safe_metadata: dict | None,
    code: EmailErrorCode,
    now: datetime,
) -> EmailMessage:
    if row is None:
        row = EmailMessage(
            id=uuid.uuid4(),
            organization_id=organization_id,
            thread_id=thread_id,
            direction=EmailDirection.OUTBOUND,
            status=EmailMessageStatus.QUEUED,
            idempotency_key=idempotency_key,
            from_address=from_address,
            to_addresses=[to_address],
            subject=sanitize_header(outbound.subject, max_len=MAX_SUBJECT_LENGTH),
            body_text=outbound.text_body,
            body_html_sanitized=outbound.html_body,
            classification=outbound.classification,
            template_id=outbound.template_id,
            campaign_id=outbound.campaign_id,
            safe_metadata=safe_metadata,
        )
        db.add(row)
    row.status = EmailMessageStatus.FAILED
    row.error_code = code.value
    row.failed_at = now
    db.commit()
    return row


def _claim_queued(db: Session, row: EmailMessage) -> bool:
    result = db.execute(
        update(EmailMessage)
        .where(EmailMessage.id == row.id, EmailMessage.status == EmailMessageStatus.QUEUED)
        .values(status=EmailMessageStatus.SENDING)
    )
    db.commit()
    return result.rowcount == 1  # type: ignore[attr-defined]


def send_email(
    db: Session,
    *,
    organization_id: uuid.UUID,
    connection: EmailConnection,
    customer: Customer,
    thread: EmailThread,
    outbound: OutboundEmail,
    idempotency_key: str,
    provider: EmailProvider,
    sender_type: SenderType = SenderType.SYSTEM,
    sender_user_id: uuid.UUID | None = None,
    safe_metadata: dict | None = None,
    now: datetime | None = None,
) -> EmailMessage:
    now = now or datetime.now(UTC)
    to_address = sanitize_header(customer.email or "", max_len=320)
    existing = _existing(db, organization_id, idempotency_key)
    if existing is not None and existing.status != EmailMessageStatus.QUEUED:
        return existing

    code = _base_policy_block(
        connection=connection, customer=customer, classification=outbound.classification
    )
    if code is None and not to_address:
        code = EmailErrorCode.INVALID_RECIPIENT
    if code is not None:
        return _write_failed(
            db,
            existing,
            organization_id=organization_id,
            thread_id=thread.id,
            idempotency_key=idempotency_key,
            from_address=connection.from_address,
            to_address=to_address,
            outbound=outbound,
            safe_metadata=safe_metadata,
            code=code,
            now=now,
        )

    text_body, html_body = _with_unsubscribe_footer(
        outbound, organization_id=organization_id, customer_id=customer.id
    )

    row = existing
    if row is None:
        row = EmailMessage(
            id=uuid.uuid4(),
            organization_id=organization_id,
            thread_id=thread.id,
            direction=EmailDirection.OUTBOUND,
            status=EmailMessageStatus.QUEUED,
            idempotency_key=idempotency_key,
            from_address=connection.from_address,
            to_addresses=[to_address],
            subject=sanitize_header(outbound.subject, max_len=MAX_SUBJECT_LENGTH),
            in_reply_to_header=outbound.in_reply_to_header,
            references_header=outbound.references_header,
            body_text=text_body,
            body_html_sanitized=html_body,
            classification=outbound.classification,
            template_id=outbound.template_id,
            campaign_id=outbound.campaign_id,
            sender_type=sender_type,
            sender_user_id=sender_user_id,
            safe_metadata=safe_metadata,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = _existing(db, organization_id, idempotency_key)
            if row is None:
                raise
            if row.status != EmailMessageStatus.QUEUED:
                return row

    if not _claim_queued(db, row):
        db.refresh(row)
        return row

    headers: dict[str, str] = {}
    if outbound.in_reply_to_header:
        headers["In-Reply-To"] = outbound.in_reply_to_header
    if outbound.references_header:
        headers["References"] = outbound.references_header
    request = SendRequest(
        to=to_address,
        from_address=connection.from_address,
        from_name=connection.from_name or "",
        reply_to=connection.reply_to_address or "",
        subject=row.subject or "",
        text_body=text_body,
        html_body=html_body,
        idempotency_key=idempotency_key,
        headers=headers,
    )
    try:
        result: SendResult = provider.send(request)
    except EmailError as exc:
        if exc.retryable:
            db.execute(
                update(EmailMessage)
                .where(EmailMessage.id == row.id, EmailMessage.status == EmailMessageStatus.SENDING)
                .values(status=EmailMessageStatus.QUEUED)
            )
        else:
            db.execute(
                update(EmailMessage)
                .where(EmailMessage.id == row.id)
                .values(status=EmailMessageStatus.FAILED, error_code=exc.code.value, failed_at=now)
            )
        db.commit()
        raise
    db.execute(
        update(EmailMessage)
        .where(EmailMessage.id == row.id)
        .values(
            status=EmailMessageStatus.SENT,
            provider_message_id=result.provider_message_id,
            sent_at=now,
        )
    )
    thread.last_outbound_at = now
    thread.message_count += 1
    db.commit()
    db.refresh(row)
    return row


def cancel_orphaned_sending(
    db: Session, *, stale_after_seconds: int = 120, limit: int = 100
) -> int:
    """Dashboard hygiene only: a row stuck SENDING with no resolution is marked FAILED after a
    generous staleness window. Never triggers a retry or a second provider call (mirrors
    ``messaging/whatsapp/send.py:cancel_orphaned_sending``)."""
    cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
    ids = list(
        db.scalars(
            select(EmailMessage.id)
            .where(
                EmailMessage.status == EmailMessageStatus.SENDING, EmailMessage.created_at < cutoff
            )
            .limit(limit)
        )
    )
    if not ids:
        return 0
    db.execute(
        update(EmailMessage)
        .where(EmailMessage.id.in_(ids))
        .values(
            status=EmailMessageStatus.FAILED, error_code="worker_lost", failed_at=datetime.now(UTC)
        )
    )
    db.commit()
    return len(ids)
