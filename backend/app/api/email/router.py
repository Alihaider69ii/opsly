"""Email templates, automations-usage and campaigns dashboard API (Phase 9). Reading is open to
every member; writes (template create/edit/status, campaign create/start/pause/resume/cancel)
need OWNER or ADMIN (campaigns send real email to potentially many customers, so this is treated
like WhatsApp template sync -- a managed action, not an everyday support operation)."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.api.deps import CurrentOrg, DbSession, require_current_org_role
from app.core.config import Settings, get_settings
from app.db.models import (
    AutomationWorkflow,
    Customer,
    EmailCampaign,
    EmailCampaignStatus,
    EmailTemplate,
    EmailTemplateStatus,
    OrganizationRole,
)
from app.schemas.email import (
    AutomationUsageOut,
    EmailCampaignIn,
    EmailCampaignOut,
    EmailTemplateIn,
    EmailTemplateOut,
    EmailTemplateStatusIn,
)
from app.services.email import campaigns as campaigns_service
from app.services.email import consent as consent_service
from app.services.email import templates as templates_service
from app.services.email import unsubscribe as unsubscribe_service
from app.services.organizations.service import OrgContext
from app.services.support import timeline as timeline_service
from app.workers.queue import EmailCampaignJob, JobQueue, get_job_queue

router = APIRouter(prefix="/email", tags=["email"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Queue = Annotated[JobQueue, Depends(get_job_queue)]
ManagerOrg = Annotated[
    OrgContext, Depends(require_current_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN))
]
ACTION_TYPES_SHOWN = ("send_email", "send_email_template")


# --- templates -----------------------------------------------------------------------------------


@router.get("/templates", response_model=list[EmailTemplateOut])
def list_templates(ctx: CurrentOrg, db: DbSession) -> list[EmailTemplate]:
    return list(
        db.scalars(
            select(EmailTemplate)
            .where(EmailTemplate.organization_id == ctx.organization.id)
            .order_by(EmailTemplate.updated_at.desc())
            .limit(500)
        )
    )


def _template(db: DbSession, ctx: OrgContext, template_id: uuid.UUID) -> EmailTemplate:
    row = db.scalar(
        select(EmailTemplate).where(
            EmailTemplate.id == template_id, EmailTemplate.organization_id == ctx.organization.id
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template not found")
    return row


def _validate_body(body: EmailTemplateIn) -> None:
    try:
        templates_service.validate_variables(
            body.variables, subject=body.subject, body_html=body.body_html, body_text=body.body_text
        )
    except templates_service.InvalidTemplate as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None


@router.post("/templates", response_model=EmailTemplateOut, status_code=status.HTTP_201_CREATED)
def create_template(body: EmailTemplateIn, ctx: ManagerOrg, db: DbSession) -> EmailTemplate:
    _validate_body(body)
    template = EmailTemplate(
        id=uuid.uuid4(),
        organization_id=ctx.organization.id,
        name=body.name,
        type=body.type,
        subject=body.subject,
        preheader=body.preheader,
        body_html=body.body_html,
        body_text=body.body_text,
        variables=body.variables,
        status=EmailTemplateStatus.DRAFT,
        created_by=ctx.user.id,
    )
    db.add(template)
    db.commit()
    return template


@router.get("/templates/{template_id}", response_model=EmailTemplateOut)
def get_template(template_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> EmailTemplate:
    return _template(db, ctx, template_id)


@router.patch("/templates/{template_id}", response_model=EmailTemplateOut)
def update_template(
    template_id: uuid.UUID, body: EmailTemplateIn, ctx: ManagerOrg, db: DbSession
) -> EmailTemplate:
    _validate_body(body)
    template = _template(db, ctx, template_id)
    template.name = body.name
    template.type = body.type
    template.subject = body.subject
    template.preheader = body.preheader
    template.body_html = body.body_html
    template.body_text = body.body_text
    template.variables = body.variables
    template.version += 1
    db.commit()
    return template


@router.post("/templates/{template_id}/status", response_model=EmailTemplateOut)
def set_template_status(
    template_id: uuid.UUID, body: EmailTemplateStatusIn, ctx: ManagerOrg, db: DbSession
) -> EmailTemplate:
    template = _template(db, ctx, template_id)
    template.status = body.status
    db.commit()
    return template


@router.post("/templates/{template_id}/preview")
def preview_template(
    template_id: uuid.UUID, ctx: CurrentOrg, db: DbSession, context: dict[str, Any] | None = None
) -> dict[str, str]:
    template = _template(db, ctx, template_id)
    subject, html_body, text_body = templates_service.render_template(
        subject=template.subject,
        body_html=template.body_html,
        body_text=template.body_text,
        context=context or {},
    )
    return {"subject": subject, "html": html_body, "text": text_body}


# --- automation usage (read-only summary) --------------------------------------------------------


@router.get("/automation-usage", response_model=list[AutomationUsageOut])
def automation_usage(ctx: CurrentOrg, db: DbSession) -> list[AutomationUsageOut]:
    rows = list(
        db.scalars(
            select(AutomationWorkflow).where(
                AutomationWorkflow.organization_id == ctx.organization.id
            )
        )
    )
    usage = []
    for row in rows:
        actions = [
            action.get("type")
            for action in row.definition.get("actions", [])
            if action.get("type") in ACTION_TYPES_SHOWN
        ]
        if actions:
            usage.append(
                AutomationUsageOut(
                    workflow_id=row.id,
                    name=row.name,
                    status=row.status.value,
                    email_actions=actions,
                )
            )
    return usage


# --- campaigns -------------------------------------------------------------------------------


@router.get("/campaigns", response_model=list[EmailCampaignOut])
def list_campaigns(ctx: CurrentOrg, db: DbSession) -> list[EmailCampaign]:
    return list(
        db.scalars(
            select(EmailCampaign)
            .where(EmailCampaign.organization_id == ctx.organization.id)
            .order_by(EmailCampaign.updated_at.desc())
            .limit(200)
        )
    )


def _campaign(db: DbSession, ctx: OrgContext, campaign_id: uuid.UUID) -> EmailCampaign:
    row = db.scalar(
        select(EmailCampaign).where(
            EmailCampaign.id == campaign_id, EmailCampaign.organization_id == ctx.organization.id
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    return row


@router.post("/campaigns", response_model=EmailCampaignOut, status_code=status.HTTP_201_CREATED)
def create_campaign(body: EmailCampaignIn, ctx: ManagerOrg, db: DbSession) -> EmailCampaign:
    template = db.scalar(
        select(EmailTemplate).where(
            EmailTemplate.id == body.template_id,
            EmailTemplate.organization_id == ctx.organization.id,
        )
    )
    if template is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Template not found")
    campaign = EmailCampaign(
        id=uuid.uuid4(),
        organization_id=ctx.organization.id,
        name=body.name,
        template_id=body.template_id,
        audience_definition=body.audience_definition,
        status=EmailCampaignStatus.SCHEDULED if body.scheduled_at else EmailCampaignStatus.DRAFT,
        scheduled_at=body.scheduled_at,
        created_by=ctx.user.id,
    )
    db.add(campaign)
    db.commit()
    return campaign


@router.get("/campaigns/{campaign_id}", response_model=EmailCampaignOut)
def get_campaign(campaign_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> EmailCampaign:
    return _campaign(db, ctx, campaign_id)


@router.post("/campaigns/{campaign_id}/start", response_model=EmailCampaignOut)
def start_campaign(
    campaign_id: uuid.UUID, ctx: ManagerOrg, db: DbSession, queue: Queue
) -> EmailCampaign:
    campaign = _campaign(db, ctx, campaign_id)
    if campaign.status not in (EmailCampaignStatus.DRAFT, EmailCampaignStatus.SCHEDULED):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Campaign cannot be started from its current status"
        )
    campaigns_service.queue_campaign(db, queue, campaign=campaign)
    return campaign


@router.post("/campaigns/{campaign_id}/pause", response_model=EmailCampaignOut)
def pause_campaign(campaign_id: uuid.UUID, ctx: ManagerOrg, db: DbSession) -> EmailCampaign:
    campaign = _campaign(db, ctx, campaign_id)
    if campaign.status != EmailCampaignStatus.RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Campaign is not running")
    campaign.status = EmailCampaignStatus.PAUSED
    db.commit()
    return campaign


@router.post("/campaigns/{campaign_id}/resume", response_model=EmailCampaignOut)
def resume_campaign(
    campaign_id: uuid.UUID, ctx: ManagerOrg, db: DbSession, queue: Queue
) -> EmailCampaign:
    campaign = _campaign(db, ctx, campaign_id)
    if campaign.status != EmailCampaignStatus.PAUSED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Campaign is not paused")
    campaign.status = EmailCampaignStatus.RUNNING
    db.commit()
    queue.enqueue(EmailCampaignJob(campaign.id))
    return campaign


@router.post("/campaigns/{campaign_id}/cancel", response_model=EmailCampaignOut)
def cancel_campaign(campaign_id: uuid.UUID, ctx: ManagerOrg, db: DbSession) -> EmailCampaign:
    campaign = _campaign(db, ctx, campaign_id)
    if campaign.status in (EmailCampaignStatus.COMPLETED, EmailCampaignStatus.CANCELLED):
        raise HTTPException(status.HTTP_409_CONFLICT, "Campaign already finished")
    campaign.status = EmailCampaignStatus.CANCELLED
    db.commit()
    return campaign


# --- one-click unsubscribe (public, no auth -- see services/email/unsubscribe.py) ----------------

_UNSUBSCRIBE_PAGE = """\
<!doctype html><html><head><meta charset="utf-8"><title>Unsubscribed</title></head>
<body><p>{message}</p></body></html>\
"""


@router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(token: str, db: DbSession, settings: AppSettings) -> HTMLResponse:
    if not settings.secret_encryption_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Unsubscribe is not configured")
    key = unsubscribe_service.derive_key(settings.secret_encryption_key.get_secret_value())
    resolved = unsubscribe_service.verify_token(token, key=key)
    if resolved is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This unsubscribe link is invalid")
    organization_id, customer_id = resolved
    customer = db.scalar(
        select(Customer).where(
            Customer.id == customer_id, Customer.organization_id == organization_id
        )
    )
    if customer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This unsubscribe link is invalid")
    now = datetime.now(UTC)
    if not customer.email_suppressed:
        consent_service.suppress(customer, reason="unsubscribed", now=now)
        timeline_service.record_event(
            db,
            organization_id=organization_id,
            event_type="email_unsubscribed",
            customer_id=customer.id,
            now=now,
        )
        db.commit()
    return HTMLResponse(
        _UNSUBSCRIBE_PAGE.format(message="You've been unsubscribed from marketing email.")
    )
