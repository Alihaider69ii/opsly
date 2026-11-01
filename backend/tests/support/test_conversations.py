import uuid
from datetime import UTC, datetime

from app.db.models import (
    AIHandoffState,
    PrioritySource,
    SenderType,
    SupportChannel,
    SupportPriority,
    SupportStatus,
)
from app.services.support import conversations as support_service
from app.workers.queue import InMemoryJobQueue
from tests.support.conftest import make_member

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _sync(db, org_id, *, channel_id=None, ai_state=AIHandoffState.AI_ACTIVE, direction="inbound", sender_type=SenderType.CUSTOMER, event_type="website_message", now=NOW):
    return support_service.sync_conversation(
        db,
        organization_id=org_id,
        channel=SupportChannel.WEBSITE,
        channel_conversation_id=channel_id or uuid.uuid4(),
        ai_state=ai_state,
        direction=direction,
        sender_type=sender_type,
        event_type=event_type,
        now=now,
    )


# --- priority: human-set is never silently overwritten --------------------------------------


def test_ai_recommended_priority_applies_when_nothing_set_yet(db, org_id):
    conversation = _sync(db, org_id)
    db.commit()
    ok = support_service.set_priority(
        db, conversation=conversation, priority=SupportPriority.HIGH, source=PrioritySource.AI
    )
    assert ok is True
    assert conversation.priority == SupportPriority.HIGH
    assert conversation.priority_source == PrioritySource.AI


def test_human_set_priority_is_never_overwritten_by_ai_or_system(db, org_id, session_factory):
    conversation = _sync(db, org_id)
    db.commit()
    user_id = make_member(session_factory, org_id)
    support_service.set_priority(
        db,
        conversation=conversation,
        priority=SupportPriority.URGENT,
        source=PrioritySource.HUMAN,
        changed_by=user_id,
    )
    db.commit()

    blocked = support_service.set_priority(
        db, conversation=conversation, priority=SupportPriority.LOW, source=PrioritySource.AI
    )
    assert blocked is False
    assert conversation.priority == SupportPriority.URGENT  # unchanged


def test_a_second_human_can_override_a_human_set_priority(db, org_id):
    conversation = _sync(db, org_id)
    db.commit()
    support_service.set_priority(
        db, conversation=conversation, priority=SupportPriority.URGENT, source=PrioritySource.HUMAN
    )
    db.commit()
    ok = support_service.set_priority(
        db, conversation=conversation, priority=SupportPriority.LOW, source=PrioritySource.HUMAN
    )
    assert ok is True
    assert conversation.priority == SupportPriority.LOW


# --- assignment: claim is race-safe, release clears it ---------------------------------------


def test_claim_succeeds_for_an_unassigned_conversation(db, org_id, session_factory):
    agent = make_member(session_factory, org_id)
    conversation = _sync(db, org_id)
    db.commit()
    assert support_service.claim(db, conversation=conversation, agent_user_id=agent) is True
    assert conversation.assigned_agent_user_id == agent


def test_two_agents_claiming_the_same_conversation_only_one_wins(db, org_id, session_factory):
    """Brief section 30: two agents claiming the same conversation must never both succeed."""
    agent_a = make_member(session_factory, org_id)
    agent_b = make_member(session_factory, org_id)
    conversation = _sync(db, org_id)
    db.commit()

    first = support_service.claim(db, conversation=conversation, agent_user_id=agent_a)
    second = support_service.claim(db, conversation=conversation, agent_user_id=agent_b)

    assert first is True
    assert second is False
    assert conversation.assigned_agent_user_id == agent_a


def test_claim_rejects_a_user_who_is_not_an_organization_member(db, org_id):
    conversation = _sync(db, org_id)
    db.commit()
    stranger = uuid.uuid4()
    try:
        support_service.claim(db, conversation=conversation, agent_user_id=stranger)
        raised = False
    except support_service.NotOrganizationMember:
        raised = True
    assert raised is True


def test_release_clears_assignment(db, org_id, session_factory):
    agent = make_member(session_factory, org_id)
    conversation = _sync(db, org_id)
    db.commit()
    support_service.claim(db, conversation=conversation, agent_user_id=agent)
    db.commit()
    support_service.release(db, conversation=conversation)
    assert conversation.assigned_agent_user_id is None


# --- AI / human switching (website channel: this row is authoritative) ------------------------


def test_take_over_moves_website_conversation_to_human_active_and_assigns_if_unassigned(
    db, org_id, session_factory
):
    agent = make_member(session_factory, org_id)
    conversation = _sync(db, org_id)
    db.commit()
    queue = InMemoryJobQueue()

    ok = support_service.take_over(db, queue, conversation=conversation, by_user_id=agent, now=NOW)

    assert ok is True
    assert conversation.ai_state == AIHandoffState.HUMAN_ACTIVE
    assert conversation.status == SupportStatus.HUMAN_ACTIVE
    assert conversation.assigned_agent_user_id == agent


def test_return_to_ai_is_a_no_op_when_already_ai_active(db, org_id):
    conversation = _sync(db, org_id)
    db.commit()
    queue = InMemoryJobQueue()
    assert support_service.return_to_ai(db, queue, conversation=conversation, now=NOW) is False


def test_return_to_ai_resumes_ai_after_takeover(db, org_id, session_factory):
    agent = make_member(session_factory, org_id)
    conversation = _sync(db, org_id)
    db.commit()
    queue = InMemoryJobQueue()
    support_service.take_over(db, queue, conversation=conversation, by_user_id=agent, now=NOW)
    db.commit()

    ok = support_service.return_to_ai(db, queue, conversation=conversation, now=NOW)

    assert ok is True
    assert conversation.ai_state == AIHandoffState.AI_ACTIVE
    assert conversation.status == SupportStatus.AI_ACTIVE


def test_request_human_flags_without_assigning_anyone(db, org_id):
    conversation = _sync(db, org_id)
    db.commit()
    queue = InMemoryJobQueue()
    ok = support_service.request_human(
        db, queue, conversation=conversation, reason="customer_requested_human", now=NOW
    )
    assert ok is True
    assert conversation.ai_state == AIHandoffState.HUMAN_REQUIRED
    assert conversation.assigned_agent_user_id is None


# --- resolve / reopen --------------------------------------------------------------------------


def test_resolve_sets_resolved_status_and_timestamp(db, org_id):
    conversation = _sync(db, org_id)
    db.commit()
    queue = InMemoryJobQueue()
    support_service.resolve_conversation(db, queue, conversation=conversation, by_user_id=None, now=NOW)
    assert conversation.status == SupportStatus.RESOLVED
    assert conversation.resolved_at == NOW


def test_reopen_is_a_no_op_unless_the_conversation_is_closed_out(db, org_id):
    conversation = _sync(db, org_id)
    db.commit()
    assert support_service.reopen_conversation(db, conversation=conversation, by_user_id=None, now=NOW) is False


def test_a_new_customer_message_auto_reopens_a_resolved_conversation(db, org_id):
    conversation = _sync(db, org_id)
    db.commit()
    queue = InMemoryJobQueue()
    support_service.resolve_conversation(db, queue, conversation=conversation, by_user_id=None, now=NOW)
    db.commit()
    assert conversation.status == SupportStatus.RESOLVED

    _sync(db, org_id, channel_id=conversation.channel_conversation_id, direction="inbound")
    db.commit()
    assert conversation.status != SupportStatus.RESOLVED
