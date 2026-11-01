import uuid
from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.db.models import (
    CustomerStatus,
    EmailCampaign,
    EmailCampaignRecipientStatus,
    EmailCampaignStatus,
    EmailClassification,
    EmailConnection,
    EmailTemplate,
    EmailTemplateStatus,
)
from app.services.email import campaigns as campaigns_service
from app.services.email import consent as consent_service
from app.services.email.providers.fake import FakeEmailProvider
from app.workers.queue import InMemoryJobQueue
from tests.email.conftest import seed_connection, seed_customer

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _active_opted_in_customer(session_factory, org_id, **kwargs):
    return seed_customer(
        session_factory,
        org_id,
        status=CustomerStatus.ACTIVE,
        email_marketing_consent=True,
        **kwargs,
    )


@pytest.fixture()
def template(db, org_id):
    row = EmailTemplate(
        id=uuid.uuid4(),
        organization_id=org_id,
        name="Welcome",
        type=EmailClassification.MARKETING,
        subject="Hi {{customer.first_name}}",
        body_html="<p>Hi {{customer.first_name}}</p>",
        body_text="Hi {{customer.first_name}}",
        variables=["customer.first_name"],
        status=EmailTemplateStatus.ACTIVE,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def campaign(db, org_id, template):
    row = EmailCampaign(
        id=uuid.uuid4(),
        organization_id=org_id,
        name="Spring sale",
        template_id=template.id,
        audience_definition={},
        status=EmailCampaignStatus.DRAFT,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def connection(db, session_factory, org_id):
    connection_id = seed_connection(session_factory, org_id)
    return db.get(EmailConnection, connection_id)


# --- audience resolution -------------------------------------------------------------------------


def test_resolve_audience_requires_marketing_consent_by_default(db, org_id, session_factory):
    _active_opted_in_customer(session_factory, org_id, email="opted-in@example.com")
    seed_customer(session_factory, org_id, status=CustomerStatus.ACTIVE, email="not-opted-in@example.com")

    audience = campaigns_service.resolve_audience(db, organization_id=org_id, audience_definition={})
    assert [c.email for c in audience] == ["opted-in@example.com"]


def test_resolve_audience_excludes_suppressed_customers_even_if_opted_in(db, org_id, session_factory):
    customer_id = _active_opted_in_customer(session_factory, org_id, email="suppressed@example.com")
    with session_factory() as fresh:
        from app.db.models import Customer

        customer = fresh.get(Customer, customer_id)
        consent_service.suppress(customer, reason="bounced", now=NOW)
        fresh.commit()

    audience = campaigns_service.resolve_audience(db, organization_id=org_id, audience_definition={})
    assert audience == []


def test_resolve_audience_excludes_archived_customers(db, org_id, session_factory):
    seed_customer(
        session_factory,
        org_id,
        status=CustomerStatus.ARCHIVED,
        email_marketing_consent=True,
        email="archived@example.com",
    )
    audience = campaigns_service.resolve_audience(db, organization_id=org_id, audience_definition={})
    assert audience == []


def test_resolve_audience_tag_filter_requires_at_least_one_matching_tag(db, org_id, session_factory):
    _active_opted_in_customer(session_factory, org_id, email="vip@example.com", tags=["vip"])
    _active_opted_in_customer(session_factory, org_id, email="regular@example.com", tags=["regular"])

    audience = campaigns_service.resolve_audience(
        db, organization_id=org_id, audience_definition={"tags": ["vip"]}
    )
    assert [c.email for c in audience] == ["vip@example.com"]


# --- queue_campaign idempotency ------------------------------------------------------------------


def test_queue_campaign_creates_one_recipient_row_per_eligible_customer(db, org_id, session_factory, campaign):
    _active_opted_in_customer(session_factory, org_id, email="a@example.com")
    _active_opted_in_customer(session_factory, org_id, email="b@example.com")
    queue = InMemoryJobQueue()

    created = campaigns_service.queue_campaign(db, queue, campaign=campaign)

    assert created == 2
    assert campaign.status == EmailCampaignStatus.RUNNING
    assert campaign.total_recipients == 2
    assert len(queue.jobs) == 1


def test_queue_campaign_called_twice_never_duplicates_recipient_rows(db, org_id, session_factory, campaign):
    """Brief section 30: duplicate campaign execution must never double-queue a recipient."""
    _active_opted_in_customer(session_factory, org_id, email="a@example.com")
    queue = InMemoryJobQueue()

    first = campaigns_service.queue_campaign(db, queue, campaign=campaign)
    second = campaigns_service.queue_campaign(db, queue, campaign=campaign)

    assert first == 1
    assert second == 0  # already-existing recipient row is never recreated
    from app.db.models import EmailCampaignRecipient

    count = db.query(EmailCampaignRecipient).filter_by(campaign_id=campaign.id).count()
    assert count == 1


# --- batch claim race safety ----------------------------------------------------------------------


def test_claim_batch_never_returns_the_same_recipient_twice(db, org_id, session_factory, campaign):
    _active_opted_in_customer(session_factory, org_id, email="a@example.com")
    _active_opted_in_customer(session_factory, org_id, email="b@example.com")
    queue = InMemoryJobQueue()
    campaigns_service.queue_campaign(db, queue, campaign=campaign)

    first_batch = campaigns_service._claim_batch(db, campaign.id, limit=10)
    second_batch = campaigns_service._claim_batch(db, campaign.id, limit=10)

    assert len(first_batch) == 2
    assert second_batch == []  # already QUEUED, not PENDING -- nothing left to claim


# --- run_campaign_batch end to end ----------------------------------------------------------------


def test_run_campaign_batch_sends_to_every_recipient_and_completes(
    db, org_id, session_factory, campaign, connection
):
    _active_opted_in_customer(session_factory, org_id, email="a@example.com", first_name="Alice")
    _active_opted_in_customer(session_factory, org_id, email="b@example.com", first_name="Bob")
    queue = InMemoryJobQueue()
    campaigns_service.queue_campaign(db, queue, campaign=campaign)

    outcome = campaigns_service.run_campaign_batch(
        db, queue, campaign_id=campaign.id, settings=get_settings(), provider=FakeEmailProvider()
    )

    assert outcome == "completed"
    db.refresh(campaign)
    assert campaign.status == EmailCampaignStatus.COMPLETED
    assert campaign.sent_count == 2

    from app.db.models import EmailCampaignRecipient

    recipients = db.query(EmailCampaignRecipient).filter_by(campaign_id=campaign.id).all()
    assert all(r.status == EmailCampaignRecipientStatus.SENT for r in recipients)


def test_run_campaign_batch_re_enqueues_itself_when_recipients_remain(
    db, org_id, session_factory, campaign, connection, monkeypatch
):
    for i in range(3):
        _active_opted_in_customer(session_factory, org_id, email=f"person{i}@example.com")
    queue = InMemoryJobQueue()
    campaigns_service.queue_campaign(db, queue, campaign=campaign)
    queue.jobs.clear()

    settings = get_settings().model_copy(update={"email_campaign_batch_size": 1})
    outcome = campaigns_service.run_campaign_batch(
        db, queue, campaign_id=campaign.id, settings=settings, provider=FakeEmailProvider()
    )

    assert outcome == "continued"
    assert len(queue.jobs) == 1  # re-queued itself for the remaining recipients
    db.refresh(campaign)
    assert campaign.status == EmailCampaignStatus.RUNNING


def test_run_campaign_batch_skips_a_recipient_who_unsubscribes_while_queued(
    db, org_id, session_factory, campaign, connection
):
    """Brief section 30: unsubscribe-while-queued race -- the policy gate must catch this at
    actual send time, not just at audience-resolution time."""
    from app.db.models import Customer

    customer_id = _active_opted_in_customer(session_factory, org_id, email="a@example.com")
    queue = InMemoryJobQueue()
    campaigns_service.queue_campaign(db, queue, campaign=campaign)

    with session_factory() as fresh:
        customer = fresh.get(Customer, customer_id)
        consent_service.suppress(customer, reason="unsubscribed", now=NOW)
        fresh.commit()

    outcome = campaigns_service.run_campaign_batch(
        db, queue, campaign_id=campaign.id, settings=get_settings(), provider=FakeEmailProvider()
    )

    assert outcome == "completed"
    db.refresh(campaign)
    assert campaign.sent_count == 0
    assert campaign.failed_count == 1

    from app.db.models import EmailCampaignRecipient

    recipient = db.query(EmailCampaignRecipient).filter_by(campaign_id=campaign.id).one()
    assert recipient.status == EmailCampaignRecipientStatus.SKIPPED_SUPPRESSED


def test_run_campaign_batch_cancels_when_no_connected_email_connection_exists(
    db, org_id, session_factory, campaign
):
    """No `connection` fixture used here -- the organization has never connected email."""
    _active_opted_in_customer(session_factory, org_id, email="a@example.com")
    queue = InMemoryJobQueue()
    campaigns_service.queue_campaign(db, queue, campaign=campaign)

    outcome = campaigns_service.run_campaign_batch(
        db, queue, campaign_id=campaign.id, settings=get_settings(), provider=FakeEmailProvider()
    )

    assert outcome == "cancelled"
    db.refresh(campaign)
    assert campaign.status == EmailCampaignStatus.CANCELLED


def test_run_campaign_batch_is_a_no_op_for_a_campaign_not_in_a_runnable_state(
    db, org_id, campaign, connection
):
    campaign.status = EmailCampaignStatus.CANCELLED
    db.commit()
    queue = InMemoryJobQueue()

    outcome = campaigns_service.run_campaign_batch(
        db, queue, campaign_id=campaign.id, settings=get_settings(), provider=FakeEmailProvider()
    )
    assert outcome == "skipped"
