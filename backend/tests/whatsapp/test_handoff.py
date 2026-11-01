from datetime import UTC, datetime

from sqlalchemy import select

from app.db.models import AutomationEvent, WhatsAppAIState
from app.services.messaging.whatsapp import handoff as handoff_service
from app.services.messaging.whatsapp import window as window_service
from app.services.messaging.whatsapp.contacts import get_or_create_contact
from app.workers.queue import InMemoryJobQueue
from tests.whatsapp.conftest import seed_connection

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _conversation(db, session_factory, org_id, secrets_manager):
    connection_id = seed_connection(session_factory, org_id, secrets_manager)
    contact = get_or_create_contact(db, organization_id=org_id, phone_e164="+14155551234")
    db.commit()
    conversation = window_service.get_or_create_conversation(
        db, organization_id=org_id, contact_id=contact.id, connection_id=connection_id
    )
    db.commit()
    return conversation


def test_trigger_handoff_moves_to_human_required_and_records_the_reason(
    db, session_factory, org_id, secrets_manager
):
    conversation = _conversation(db, session_factory, org_id, secrets_manager)
    queue = InMemoryJobQueue()

    triggered = handoff_service.trigger_handoff(
        db, queue, conversation=conversation, reason="customer_requested_human", now=NOW
    )

    assert triggered is True
    assert conversation.ai_state == WhatsAppAIState.HUMAN_REQUIRED
    assert conversation.handoff_reason == "customer_requested_human"
    # SQLite drops timezone metadata on the round trip through db.refresh(); interpret as UTC.
    assert conversation.handoff_at.replace(tzinfo=UTC) == NOW
    assert handoff_service.is_ai_active(conversation) is False


def test_trigger_handoff_emits_an_auditable_automation_event(
    db, session_factory, org_id, secrets_manager
):
    conversation = _conversation(db, session_factory, org_id, secrets_manager)
    queue = InMemoryJobQueue()
    handoff_service.trigger_handoff(
        db, queue, conversation=conversation, reason="max_replies_reached", now=NOW
    )
    events = list(
        db.scalars(select(AutomationEvent).where(AutomationEvent.organization_id == org_id))
    )
    assert len(events) == 1
    assert events[0].event_type == "whatsapp_handoff_requested"
    assert events[0].payload["whatsapp"]["reason"] == "max_replies_reached"
    assert len(queue.jobs) == 1  # the event was enqueued for the automation dispatcher


def test_a_second_trigger_is_a_no_op_and_does_not_overwrite_the_reason(
    db, session_factory, org_id, secrets_manager
):
    conversation = _conversation(db, session_factory, org_id, secrets_manager)
    queue = InMemoryJobQueue()
    handoff_service.trigger_handoff(
        db, queue, conversation=conversation, reason="customer_requested_human", now=NOW
    )
    second = handoff_service.trigger_handoff(
        db, queue, conversation=conversation, reason="repeated_fallback", now=NOW
    )
    assert second is False
    assert conversation.handoff_reason == "customer_requested_human"  # not overwritten
    events = list(
        db.scalars(select(AutomationEvent).where(AutomationEvent.organization_id == org_id))
    )
    assert len(events) == 1  # no second event


def test_claim_moves_human_required_to_human_active(db, session_factory, org_id, secrets_manager):
    conversation = _conversation(db, session_factory, org_id, secrets_manager)
    handoff_service.trigger_handoff(
        db,
        InMemoryJobQueue(),
        conversation=conversation,
        reason="customer_requested_human",
        now=NOW,
    )
    assert handoff_service.claim(db, conversation=conversation) is True
    assert conversation.ai_state == WhatsAppAIState.HUMAN_ACTIVE


def test_claim_is_a_no_op_when_not_human_required(db, session_factory, org_id, secrets_manager):
    conversation = _conversation(db, session_factory, org_id, secrets_manager)
    assert handoff_service.claim(db, conversation=conversation) is False
    assert conversation.ai_state == WhatsAppAIState.AI_ACTIVE


def test_resolve_returns_control_to_ai_and_clears_handoff_fields(
    db, session_factory, org_id, secrets_manager
):
    conversation = _conversation(db, session_factory, org_id, secrets_manager)
    handoff_service.trigger_handoff(
        db, InMemoryJobQueue(), conversation=conversation, reason="repeated_fallback", now=NOW
    )
    conversation.ai_consecutive_fallbacks = 3
    db.commit()

    resolved = handoff_service.resolve(db, conversation=conversation, now=NOW)

    assert resolved is True
    assert conversation.ai_state == WhatsAppAIState.AI_ACTIVE
    assert conversation.handoff_reason is None
    assert conversation.handoff_at is None
    assert conversation.ai_consecutive_fallbacks == 0


def test_resolve_is_a_no_op_when_already_ai_active(db, session_factory, org_id, secrets_manager):
    conversation = _conversation(db, session_factory, org_id, secrets_manager)
    assert handoff_service.resolve(db, conversation=conversation, now=NOW) is False
