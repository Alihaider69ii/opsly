"""Phase 9: email campaigns -- audience resolution and the durable, idempotent, rate-limited send
loop (brief section 19).

Idempotency is layered, the same way the rest of this codebase layers it:

1. ``EmailCampaignRecipient``'s unique ``(organization_id, campaign_id, customer_id)`` means at
   most one recipient row can ever exist for one customer in one campaign.
2. Each send's idempotency key is ``campaign:<campaign id>:<customer id>`` -- even in the
   impossible-in-practice case of two workers racing the same recipient row,
   ``services/email/send.py``'s own idempotency boundary means the provider is still called at
   most once.
3. ``services/email/send.py``'s policy gate re-checks suppression/consent at the moment of send,
   not just when the campaign was queued -- an unsubscribe or suppression that happens while a
   campaign is mid-flight is caught there, not assumed stale from audience resolution time.

A batch is time-sliced (``EMAIL_CAMPAIGN_BATCH_SIZE`` recipients per job), re-queuing itself if
more remain, mirroring the Shopify sync job's own slice-and-requeue pattern -- a huge campaign
cannot starve the worker's other queues.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import (
    Customer,
    CustomerStatus,
    EmailCampaign,
    EmailCampaignRecipient,
    EmailCampaignRecipientStatus,
    EmailCampaignStatus,
    EmailConnection,
    EmailMessageStatus,
    EmailTemplate,
    EmailTemplateStatus,
    SenderType,
    WhatsAppConnectionStatus,
)
from app.services.email import send as email_send
from app.services.email import templates as email_templates
from app.services.email import threading as email_threading
from app.services.email.errors import EmailError
from app.services.email.provider import EmailProvider
from app.workers.queue import EmailCampaignJob, JobQueue

logger = logging.getLogger(__name__)

MAX_AUDIENCE_TAGS = 20


def resolve_audience(
    db: Session, *, organization_id: uuid.UUID, audience_definition: dict
) -> list[Customer]:
    """A bounded, deterministic filter -- never raw SQL, never arbitrary code (brief section
    19). Suppressed contacts are excluded unconditionally; marketing consent is required
    unless the definition explicitly overrides it (never for a MARKETING-classified send in
    practice, since ``send.py`` re-checks consent at send time regardless)."""
    query = select(Customer).where(
        Customer.organization_id == organization_id,
        Customer.status == CustomerStatus.ACTIVE,
        Customer.email.isnot(None),
        Customer.email_suppressed.is_(False),
    )
    if audience_definition.get("marketing_consent", True):
        query = query.where(Customer.email_marketing_consent.is_(True))
    rows = list(db.scalars(query.limit(100_000)))
    tags = audience_definition.get("tags")
    if isinstance(tags, list) and tags:
        wanted = set(tags[:MAX_AUDIENCE_TAGS])
        rows = [c for c in rows if wanted & set(c.tags or [])]
    return rows


def queue_campaign(db: Session, queue: JobQueue, *, campaign: EmailCampaign) -> int:
    """Resolves the audience and creates one PENDING recipient row per eligible customer
    (idempotent: calling this twice for the same campaign never duplicates a row)."""
    audience = resolve_audience(
        db,
        organization_id=campaign.organization_id,
        audience_definition=campaign.audience_definition,
    )
    existing = set(
        db.scalars(
            select(EmailCampaignRecipient.customer_id).where(
                EmailCampaignRecipient.organization_id == campaign.organization_id,
                EmailCampaignRecipient.campaign_id == campaign.id,
            )
        )
    )
    created = 0
    for customer in audience:
        if customer.id in existing:
            continue
        db.add(
            EmailCampaignRecipient(
                id=uuid.uuid4(),
                organization_id=campaign.organization_id,
                campaign_id=campaign.id,
                customer_id=customer.id,
                email=customer.email,
                status=EmailCampaignRecipientStatus.PENDING,
            )
        )
        created += 1
    campaign.status = EmailCampaignStatus.RUNNING
    campaign.started_at = campaign.started_at or datetime.now(UTC)
    campaign.total_recipients = len(existing) + created
    db.commit()
    queue.enqueue(EmailCampaignJob(campaign.id))
    return created


def _claim_batch(
    db: Session, campaign_id: uuid.UUID, *, limit: int
) -> list[EmailCampaignRecipient]:
    ids = list(
        db.scalars(
            select(EmailCampaignRecipient.id)
            .where(
                EmailCampaignRecipient.campaign_id == campaign_id,
                EmailCampaignRecipient.status == EmailCampaignRecipientStatus.PENDING,
            )
            .order_by(EmailCampaignRecipient.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    if not ids:
        return []
    db.execute(
        update(EmailCampaignRecipient)
        .where(EmailCampaignRecipient.id.in_(ids))
        .values(status=EmailCampaignRecipientStatus.QUEUED)
    )
    db.commit()
    return list(
        db.scalars(select(EmailCampaignRecipient).where(EmailCampaignRecipient.id.in_(ids)))
    )


def run_campaign_batch(
    db: Session,
    queue: JobQueue,
    *,
    campaign_id: uuid.UUID,
    settings: Settings,
    provider: EmailProvider,
) -> str:
    campaign = db.get(EmailCampaign, campaign_id)
    if campaign is None or campaign.status not in (
        EmailCampaignStatus.RUNNING,
        EmailCampaignStatus.SCHEDULED,
    ):
        return "skipped"
    connection = db.scalar(
        select(EmailConnection).where(
            EmailConnection.organization_id == campaign.organization_id,
            EmailConnection.status == WhatsAppConnectionStatus.CONNECTED,
        )
    )
    template = db.scalar(
        select(EmailTemplate).where(
            EmailTemplate.id == campaign.template_id,
            EmailTemplate.organization_id == campaign.organization_id,
        )
    )
    if connection is None or template is None or template.status != EmailTemplateStatus.ACTIVE:
        campaign.status = EmailCampaignStatus.CANCELLED
        db.commit()
        return "cancelled"

    batch = _claim_batch(db, campaign.id, limit=settings.email_campaign_batch_size)
    if not batch:
        if campaign.status != EmailCampaignStatus.COMPLETED:
            campaign.status = EmailCampaignStatus.COMPLETED
            campaign.completed_at = datetime.now(UTC)
            db.commit()
        return "completed"

    for recipient in batch:
        customer = db.scalar(
            select(Customer).where(
                Customer.id == recipient.customer_id,
                Customer.organization_id == campaign.organization_id,
            )
        )
        if customer is None or not customer.email:
            recipient.status = EmailCampaignRecipientStatus.SKIPPED_SUPPRESSED
            recipient.error_code = "customer_missing"
            campaign.skipped_count += 1
            db.commit()
            continue
        thread = email_threading.get_or_create_outbound_thread(
            db,
            organization_id=campaign.organization_id,
            customer_id=customer.id,
            customer_email=customer.email,
            subject=template.subject,
        )
        context = {
            "customer": {
                "first_name": customer.first_name or "",
                "last_name": customer.last_name or "",
                "email": customer.email or "",
            }
        }
        subject, html_body, text_body = email_templates.render_template(
            subject=template.subject,
            body_html=template.body_html,
            body_text=template.body_text,
            context=context,
        )
        idem_key = f"campaign:{campaign.id}:{customer.id}"
        try:
            result = email_send.send_email(
                db,
                organization_id=campaign.organization_id,
                connection=connection,
                customer=customer,
                thread=thread,
                outbound=email_send.OutboundEmail(
                    subject=subject,
                    text_body=text_body,
                    html_body=html_body,
                    classification=template.type,
                    template_id=template.id,
                    campaign_id=campaign.id,
                ),
                idempotency_key=idem_key,
                provider=provider,
                sender_type=SenderType.SYSTEM,
                now=datetime.now(UTC),
            )
        except EmailError as exc:
            recipient.status = EmailCampaignRecipientStatus.FAILED
            recipient.error_code = exc.code.value
            campaign.failed_count += 1
            db.commit()
            continue
        if result.status == EmailMessageStatus.FAILED:
            if result.error_code in ("opted_out", "suppressed"):
                recipient.status = EmailCampaignRecipientStatus.SKIPPED_SUPPRESSED
            else:
                recipient.status = EmailCampaignRecipientStatus.FAILED
            recipient.error_code = result.error_code
            campaign.failed_count += 1
        else:
            recipient.status = EmailCampaignRecipientStatus.SENT
            recipient.email_message_id = result.id
            recipient.sent_at = datetime.now(UTC)
            campaign.sent_count += 1
        db.commit()

    remaining = db.scalar(
        select(EmailCampaignRecipient.id)
        .where(
            EmailCampaignRecipient.campaign_id == campaign.id,
            EmailCampaignRecipient.status == EmailCampaignRecipientStatus.PENDING,
        )
        .limit(1)
    )
    if remaining is not None:
        queue.enqueue(EmailCampaignJob(campaign.id))
        return "continued"
    campaign.status = EmailCampaignStatus.COMPLETED
    campaign.completed_at = datetime.now(UTC)
    db.commit()
    return "completed"
