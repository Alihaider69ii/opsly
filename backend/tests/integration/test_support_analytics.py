"""Postgres-only (func.extract("epoch", ...)) -- see app/services/support/analytics.py. Run with
``pytest -m integration`` against a real docker-compose Postgres (auto-marked by path, see
tests/conftest.py:pytest_collection_modifyitems).
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.db.models import (
    AIHandoffState,
    EmailDirection,
    EmailMessage,
    EmailMessageStatus,
    EmailThread,
    Organization,
    PrioritySource,
    SenderType,
    SupportChannel,
    SupportPriority,
    WhatsAppAIState,
)
from app.services.support import analytics as analytics_service
from app.services.support import conversations as support_service
from app.workers.queue import InMemoryJobQueue

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _org(db, label):
    row = Organization(name=f"Analytics {label}", slug=f"analytics-{label}-{uuid.uuid4().hex[:8]}")
    db.add(row)
    db.flush()
    return row


def _website_conversation(db, org_id, *, channel_id=None):
    return support_service.sync_conversation(
        db,
        organization_id=org_id,
        channel=SupportChannel.WEBSITE,
        channel_conversation_id=channel_id or uuid.uuid4(),
        ai_state=AIHandoffState.AI_ACTIVE,
        direction="inbound",
        sender_type=SenderType.CUSTOMER,
        event_type="website_message",
        now=NOW,
    )


def test_postgres_analytics_aggregates_priority_and_handling_counts(pg):
    organization = _org(pg, "basic")
    pg.commit()

    urgent = _website_conversation(pg, organization.id)
    support_service.set_priority(
        pg, conversation=urgent, priority=SupportPriority.URGENT, source=PrioritySource.AI
    )
    high = _website_conversation(pg, organization.id)
    support_service.set_priority(
        pg, conversation=high, priority=SupportPriority.HIGH, source=PrioritySource.AI
    )
    _website_conversation(pg, organization.id)  # a third, NORMAL-priority conversation
    pg.commit()

    stats = analytics_service.compute(pg, organization_id=organization.id)

    assert stats.total_conversations == 3
    assert stats.urgent_conversations == 1
    assert stats.high_priority_conversations == 1
    assert stats.open_conversations == 3


def test_postgres_analytics_counts_ai_vs_human_handled_by_last_responder(pg):
    organization = _org(pg, "handled")
    pg.commit()

    ai_conv = _website_conversation(pg, organization.id)
    support_service.sync_conversation(
        pg,
        organization_id=organization.id,
        channel=SupportChannel.WEBSITE,
        channel_conversation_id=ai_conv.channel_conversation_id,
        ai_state=AIHandoffState.AI_ACTIVE,
        direction="outbound",
        sender_type=SenderType.AI,
        event_type="ai_reply",
        now=NOW + timedelta(seconds=30),
    )

    human_conv = _website_conversation(pg, organization.id)
    support_service.sync_conversation(
        pg,
        organization_id=organization.id,
        channel=SupportChannel.WEBSITE,
        channel_conversation_id=human_conv.channel_conversation_id,
        ai_state=AIHandoffState.HUMAN_ACTIVE,
        direction="outbound",
        sender_type=SenderType.HUMAN,
        event_type="human_reply",
        now=NOW + timedelta(seconds=30),
    )
    pg.commit()

    stats = analytics_service.compute(pg, organization_id=organization.id)

    assert stats.ai_handled == 1
    assert stats.human_handled == 1


def test_postgres_analytics_computes_real_first_response_and_resolution_averages(pg):
    """The whole reason this module is integration-tier only: func.extract("epoch", ...) is not
    supported against the SQLite unit tier's engine. ``created_at`` is a Postgres
    ``server_default=func.now()`` (real wall-clock at insert time), so every timestamp compared
    against it here must be real-wall-clock-relative, not the fixed historical ``NOW`` constant
    used elsewhere in this file for fields the application sets explicitly."""
    organization = _org(pg, "timing")
    pg.commit()

    real_now = datetime.now(UTC)
    conversation = _website_conversation(pg, organization.id)
    pg.commit()
    support_service.sync_conversation(
        pg,
        organization_id=organization.id,
        channel=SupportChannel.WEBSITE,
        channel_conversation_id=conversation.channel_conversation_id,
        ai_state=AIHandoffState.AI_ACTIVE,
        direction="outbound",
        sender_type=SenderType.AI,
        event_type="ai_reply",
        now=real_now + timedelta(seconds=90),
    )
    pg.commit()
    queue = InMemoryJobQueue()
    support_service.resolve_conversation(
        pg, queue, conversation=conversation, by_user_id=None, now=real_now + timedelta(minutes=10)
    )
    pg.commit()

    stats = analytics_service.compute(pg, organization_id=organization.id)

    assert stats.avg_first_response_seconds is not None
    assert 89 <= stats.avg_first_response_seconds <= 91
    assert stats.avg_resolution_seconds is not None
    assert 599 <= stats.avg_resolution_seconds <= 601


def test_postgres_analytics_counts_ai_handoffs(pg):
    organization = _org(pg, "handoffs")
    pg.commit()
    conversation = _website_conversation(pg, organization.id)
    pg.commit()
    queue = InMemoryJobQueue()

    support_service.request_human(
        pg, queue, conversation=conversation, reason="customer_requested_human", now=NOW
    )
    pg.commit()

    stats = analytics_service.compute(pg, organization_id=organization.id)
    assert stats.ai_handoffs == 1


def test_postgres_analytics_counts_email_status_breakdown(pg):
    organization = _org(pg, "email")
    pg.commit()
    thread = EmailThread(
        organization_id=organization.id,
        customer_email="sam@example.com",
        ai_state=WhatsAppAIState.AI_ACTIVE,
    )
    pg.add(thread)
    pg.flush()

    def _message(status):
        return EmailMessage(
            organization_id=organization.id,
            thread_id=thread.id,
            direction=EmailDirection.OUTBOUND,
            status=status,
            idempotency_key=str(uuid.uuid4()),
            from_address="support@acme.example",
            to_addresses=["sam@example.com"],
        )

    pg.add(_message(EmailMessageStatus.DELIVERED))
    pg.add(_message(EmailMessageStatus.BOUNCED))
    pg.add(_message(EmailMessageStatus.UNSUBSCRIBED))
    pg.add(_message(EmailMessageStatus.QUEUED))  # not yet sent -- excluded from "sent"
    pg.commit()

    stats = analytics_service.compute(pg, organization_id=organization.id)

    assert stats.emails_sent == 2  # DELIVERED + BOUNCED both count as "sent"
    assert stats.emails_delivered == 1
    assert stats.emails_bounced == 1
    assert stats.emails_unsubscribed == 1


def test_postgres_analytics_never_crosses_tenants(pg):
    org_a = _org(pg, "a")
    org_b = _org(pg, "b")
    pg.commit()
    _website_conversation(pg, org_a.id)
    pg.commit()

    stats_b = analytics_service.compute(pg, organization_id=org_b.id)
    assert stats_b.total_conversations == 0
