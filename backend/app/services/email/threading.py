"""Phase 9: email threading (brief section 14).

Uses RFC 5322 ``Message-ID``/``In-Reply-To``/``References`` plus the provider's own thread id
where available. Deterministic and auditable:

* a provider-reported thread id, once seen, always wins (reused verbatim)
* otherwise, an inbound reply's ``In-Reply-To``/``References`` is matched against
  ``EmailMessage.message_id_header`` already stored for this organization -- found, it joins that
  message's thread; not found, a new thread is started
* a shared sender address is never, on its own, a reason to join two threads together (a customer
  emailing about two unrelated orders gets two threads, not one)

Callers commit; this only adds/flushes within the caller's transaction (mirrors
``app/services/messaging/whatsapp/contacts.py``).
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import EmailMessage, EmailThread

MAX_REFERENCES = 50  # a pathologically long References header is bounded, never fully trusted


def parse_references(header: str | None) -> list[str]:
    """RFC 5322 References/In-Reply-To are whitespace-separated ``<id>`` tokens, oldest first."""
    if not header:
        return []
    return [token for token in header.split() if token][:MAX_REFERENCES]


def _by_provider_thread_id(
    db: Session, *, organization_id: uuid.UUID, provider_thread_id: str
) -> EmailThread | None:
    return db.scalar(
        select(EmailThread).where(
            EmailThread.organization_id == organization_id,
            EmailThread.provider_thread_id == provider_thread_id,
        )
    )


def _by_referenced_message(
    db: Session, *, organization_id: uuid.UUID, candidate_ids: list[str]
) -> EmailThread | None:
    if not candidate_ids:
        return None
    message = db.scalar(
        select(EmailMessage).where(
            EmailMessage.organization_id == organization_id,
            EmailMessage.message_id_header.in_(candidate_ids),
        )
    )
    if message is None:
        return None
    return db.get(EmailThread, message.thread_id)


def resolve_inbound_thread(
    db: Session,
    *,
    organization_id: uuid.UUID,
    customer_id: uuid.UUID | None,
    customer_email: str,
    subject: str | None,
    provider_thread_id: str | None,
    in_reply_to_header: str | None,
    references_header: str | None,
    now: datetime,
) -> EmailThread:
    if provider_thread_id:
        thread = _by_provider_thread_id(
            db, organization_id=organization_id, provider_thread_id=provider_thread_id
        )
        if thread is not None:
            return thread
    candidates = parse_references(in_reply_to_header) + parse_references(references_header)
    thread = _by_referenced_message(db, organization_id=organization_id, candidate_ids=candidates)
    if thread is not None:
        if provider_thread_id and not thread.provider_thread_id:
            thread.provider_thread_id = provider_thread_id
        return thread
    thread = EmailThread(
        id=uuid.uuid4(),
        organization_id=organization_id,
        customer_id=customer_id,
        customer_email=customer_email[:320],
        subject=(subject or "")[:998] or None,
        provider_thread_id=provider_thread_id,
    )
    db.add(thread)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise
    return thread


def get_or_create_outbound_thread(
    db: Session,
    *,
    organization_id: uuid.UUID,
    customer_id: uuid.UUID,
    customer_email: str,
    subject: str,
    thread_id: uuid.UUID | None = None,
) -> EmailThread:
    """For staff/AI replies, pass the existing ``thread_id``. For a new outbound-initiated
    contact (a template/automation/campaign send with no prior thread), one new thread is
    created per call -- callers that want "one thread per customer for lifecycle mail" should
    look up an existing open thread themselves first; this function never does that implicitly,
    so a deliberate new conversation (e.g. a fresh campaign) is never silently merged into an old
    one."""
    if thread_id is not None:
        thread = db.scalar(
            select(EmailThread).where(
                EmailThread.id == thread_id, EmailThread.organization_id == organization_id
            )
        )
        if thread is not None:
            return thread
    thread = EmailThread(
        id=uuid.uuid4(),
        organization_id=organization_id,
        customer_id=customer_id,
        customer_email=customer_email[:320],
        subject=subject[:998] or None,
    )
    db.add(thread)
    db.flush()
    return thread
