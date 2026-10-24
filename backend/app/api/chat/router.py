"""Dashboard side of website chat: read and change this organization's widget settings."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentOrg, DbSession, require_current_org_role
from app.db.models import ChatWidget, OrganizationRole
from app.schemas.chat import WidgetOut, WidgetUpdate
from app.services.chat import service
from app.services.organizations.service import OrgContext

router = APIRouter(prefix="/chat", tags=["chat"])

ManagerOrg = Annotated[
    OrgContext,
    Depends(require_current_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN)),
]


def _out(widget: ChatWidget) -> WidgetOut:
    return WidgetOut(
        public_key=widget.public_key,
        enabled=widget.enabled,
        welcome_message=service.effective_welcome(widget),
        allowed_origins=list(widget.allowed_origins or []),
    )


@router.get("/widget", response_model=WidgetOut)
def get_widget(ctx: CurrentOrg, db: DbSession) -> WidgetOut:
    """The widget settings, created on first use. Any member may read them (the public key is not
    a secret: it is visible in the page source of any site that embeds the widget)."""
    return _out(service.get_or_create_widget(db, ctx.organization.id))


@router.patch("/widget", response_model=WidgetOut)
def update_widget(body: WidgetUpdate, ctx: ManagerOrg, db: DbSession) -> WidgetOut:
    widget = service.get_or_create_widget(db, ctx.organization.id)
    provided = body.model_fields_set
    updated = service.update_widget(
        db,
        widget,
        enabled=body.enabled,
        welcome_message=body.welcome_message or None,
        allowed_origins=body.allowed_origins,
        clear_welcome="welcome_message" in provided and not body.welcome_message,
    )
    return _out(updated)
