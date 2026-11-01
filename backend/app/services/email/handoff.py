"""Phase 9 email human handoff: the deterministic state machine deciding whether AI auto-reply
may run for a thread right now, and the triggers that move a thread out of AI control. Mirrors
``app/services/messaging/whatsapp/handoff.py`` exactly (same state machine, same atomic-claim
race safety); only the model (``EmailThread``) and the emitted automation event type differ.

    AI_ACTIVE -> HUMAN_REQUIRED (a trigger fires: explicit request, repeated fallback, max AI
    replies reached) -> HUMAN_ACTIVE (a human claims it, or takes over directly from AI_ACTIVE)
    -> AI_ACTIVE (explicit resolve)

AI auto-reply is attempted only while ``ai_state == AI_ACTIVE`` (checked in
``services/email/autoreply.py`` before any retrieval/generation happens). Never sends a fake
"a human has joined" message -- this module only owns the state transition and the audit event.
"""

import logging
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.models import EmailThread, WhatsAppAIState
from app.services.automation import ingest_event
from app.workers.queue import JobQueue

logger = logging.getLogger(__name__)

HANDOFF_EVENT_TYPE = "email_handoff_requested"


def is_ai_active(thread: EmailThread) -> bool:
    return thread.ai_state == WhatsAppAIState.AI_ACTIVE


def trigger_handoff(
    db: Session, queue: JobQueue, *, thread: EmailThread, reason: str, now: datetime
) -> bool:
    result = db.execute(
        update(EmailThread)
        .where(
            EmailThread.id == thread.id,
            EmailThread.organization_id == thread.organization_id,
            EmailThread.ai_state == WhatsAppAIState.AI_ACTIVE,
        )
        .values(ai_state=WhatsAppAIState.HUMAN_REQUIRED, handoff_reason=reason[:60], handoff_at=now)
    )
    db.commit()
    if result.rowcount != 1:  # type: ignore[attr-defined]
        return False
    db.refresh(thread)
    logger.info(
        "email handoff triggered org=%s thread=%s reason=%s",
        thread.organization_id,
        thread.id,
        reason,
    )
    ingest_event(
        db,
        queue,
        organization_id=thread.organization_id,
        event_type=HANDOFF_EVENT_TYPE,
        source="email",
        source_id=str(thread.id),
        dedupe_key=f"email:handoff:{thread.id}:{int(now.timestamp())}",
        payload={
            "event": {"source": "email"},
            "customer": {"exists": thread.customer_id is not None},
            "email": {"thread_id": str(thread.id), "reason": reason},
        },
    )
    return True


def take_over(db: Session, *, thread: EmailThread, now: datetime) -> bool:
    """A human takes over immediately, from AI_ACTIVE or HUMAN_REQUIRED alike."""
    result = db.execute(
        update(EmailThread)
        .where(
            EmailThread.id == thread.id,
            EmailThread.organization_id == thread.organization_id,
            EmailThread.ai_state.in_([WhatsAppAIState.AI_ACTIVE, WhatsAppAIState.HUMAN_REQUIRED]),
        )
        .values(ai_state=WhatsAppAIState.HUMAN_ACTIVE, handoff_at=now)
    )
    db.commit()
    if result.rowcount != 1:  # type: ignore[attr-defined]
        return False
    db.refresh(thread)
    return True


def claim(db: Session, *, thread: EmailThread) -> bool:
    if thread.ai_state != WhatsAppAIState.HUMAN_REQUIRED:
        return False
    thread.ai_state = WhatsAppAIState.HUMAN_ACTIVE
    db.commit()
    return True


def resolve(db: Session, *, thread: EmailThread, now: datetime) -> bool:
    if thread.ai_state == WhatsAppAIState.AI_ACTIVE:
        return False
    thread.ai_state = WhatsAppAIState.AI_ACTIVE
    thread.handoff_reason = None
    thread.handoff_at = None
    thread.ai_consecutive_fallbacks = 0
    db.commit()
    logger.info("email handoff resolved org=%s thread=%s", thread.organization_id, thread.id)
    return True
