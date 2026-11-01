"""Phase 8 human handoff: the deterministic state machine deciding whether AI auto-reply may run
for a conversation right now, and the triggers that move a conversation out of AI control.

    AI_ACTIVE -> HUMAN_REQUIRED (a trigger fires: explicit request, repeated fallback, max AI
    replies reached) -> HUMAN_ACTIVE (a human claims it, optional, not required before resolving)
    -> AI_ACTIVE (explicit resolve; see app/api/whatsapp/router.py)

AI auto-reply is attempted only while ``ai_state == AI_ACTIVE`` (checked in
``services/messaging/whatsapp/autoreply.py`` before any retrieval/generation happens -- this is a
cost control, not the enforcement boundary; nothing here ever calls a messaging provider).

Triggering handoff never sends a fake "a human has joined" message (Phase 8 brief section 15):
this module only owns the state transition and the audit event. The connection's configured
``ai_handoff_message`` (if any) is sent by the caller through the ordinary send policy gate,
exactly like any other AI-generated text.
"""

import logging
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.models import WhatsAppAIState, WhatsAppConversation
from app.services.automation import ingest_event
from app.workers.queue import JobQueue

logger = logging.getLogger(__name__)

HANDOFF_EVENT_TYPE = "whatsapp_handoff_requested"


def is_ai_active(conversation: WhatsAppConversation) -> bool:
    return conversation.ai_state == WhatsAppAIState.AI_ACTIVE


def trigger_handoff(
    db: Session,
    queue: JobQueue,
    *,
    conversation: WhatsAppConversation,
    reason: str,
    now: datetime,
) -> bool:
    """Moves ``conversation`` to HUMAN_REQUIRED and emits an auditable automation event
    (``whatsapp_handoff_requested``, triggerable like any other workflow event). Returns False (a
    no-op) if a handoff was already in effect, so callers never double-trigger, overwrite an
    existing reason, or send two handoff acknowledgements for two inbound messages processed
    concurrently -- the transition is an atomic ``UPDATE ... WHERE ai_state = 'AI_ACTIVE'``,
    mirroring the same claim pattern already used for queued sends (``send.py:_claim_queued``)
    and a locked chat turn (``chat/service.py:_acquire``)."""
    result = db.execute(
        update(WhatsAppConversation)
        .where(
            WhatsAppConversation.id == conversation.id,
            WhatsAppConversation.organization_id == conversation.organization_id,
            WhatsAppConversation.ai_state == WhatsAppAIState.AI_ACTIVE,
        )
        .values(ai_state=WhatsAppAIState.HUMAN_REQUIRED, handoff_reason=reason[:60], handoff_at=now)
    )
    db.commit()
    if result.rowcount != 1:  # type: ignore[attr-defined]
        return False
    db.refresh(conversation)
    logger.info(
        "whatsapp handoff triggered org=%s conversation=%s reason=%s",
        conversation.organization_id,
        conversation.id,
        reason,
    )
    ingest_event(
        db,
        queue,
        organization_id=conversation.organization_id,
        event_type=HANDOFF_EVENT_TYPE,
        source="whatsapp",
        source_id=str(conversation.id),
        dedupe_key=f"whatsapp:handoff:{conversation.id}:{int(now.timestamp())}",
        payload={
            "event": {"source": "whatsapp"},
            "customer": {"exists": True},
            "whatsapp": {
                "conversation_id": str(conversation.id),
                "contact_id": str(conversation.contact_id),
                "reason": reason,
            },
        },
    )
    return True


def take_over(db: Session, *, conversation: WhatsAppConversation, now: datetime) -> bool:
    """Phase 9: a human takes over immediately, from AI_ACTIVE or HUMAN_REQUIRED alike (the
    brief's "TAKE OVER" control works at any time, unlike Phase 8's ``claim``, which only
    promotes an existing HUMAN_REQUIRED). Same atomic-claim shape as ``trigger_handoff``, so two
    dashboard users clicking "take over" at once can never both win. No-op (False) only if the
    conversation is already HUMAN_ACTIVE or already RESOLVED."""
    result = db.execute(
        update(WhatsAppConversation)
        .where(
            WhatsAppConversation.id == conversation.id,
            WhatsAppConversation.organization_id == conversation.organization_id,
            WhatsAppConversation.ai_state.in_(
                [WhatsAppAIState.AI_ACTIVE, WhatsAppAIState.HUMAN_REQUIRED]
            ),
        )
        .values(ai_state=WhatsAppAIState.HUMAN_ACTIVE, handoff_at=now)
    )
    db.commit()
    if result.rowcount != 1:  # type: ignore[attr-defined]
        return False
    db.refresh(conversation)
    logger.info(
        "whatsapp human take-over org=%s conversation=%s",
        conversation.organization_id,
        conversation.id,
    )
    return True


def claim(db: Session, *, conversation: WhatsAppConversation) -> bool:
    """A human claims a HUMAN_REQUIRED conversation (dashboard-triggered). Not required before
    ``resolve`` -- a human may resolve directly from HUMAN_REQUIRED."""
    if conversation.ai_state != WhatsAppAIState.HUMAN_REQUIRED:
        return False
    conversation.ai_state = WhatsAppAIState.HUMAN_ACTIVE
    db.commit()
    return True


def resolve(db: Session, *, conversation: WhatsAppConversation, now: datetime) -> bool:
    """A human is done; control returns to AI. No-op (returns False) if already AI_ACTIVE."""
    if conversation.ai_state == WhatsAppAIState.AI_ACTIVE:
        return False
    conversation.ai_state = WhatsAppAIState.AI_ACTIVE
    conversation.handoff_reason = None
    conversation.handoff_at = None
    conversation.ai_consecutive_fallbacks = 0
    db.commit()
    logger.info(
        "whatsapp handoff resolved org=%s conversation=%s",
        conversation.organization_id,
        conversation.id,
    )
    return True
