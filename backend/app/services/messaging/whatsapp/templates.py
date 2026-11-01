"""Template abstraction: synced metadata plus variable validation that fails closed before any
provider call is made.

Scope decision (see docs/whatsapp.md and docs/phase-6-progress.md): this supports only positional
``{{1}}``, ``{{2}}``, ... placeholders in the template's BODY component. Meta documents TWO
current, valid template parameter formats, chosen per template at creation time via
``parameter_format``: positional (what this module supports) and named (``{{first_name}}``-style,
sent with a ``parameter_name`` field, independently order-free) -- verified against
https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview
(Phase 7). Named is a real, current, first-class Meta feature, not a deprecated or invalid one --
this module deliberately does not support it yet (a template created in the named format is
rejected rather than guessed at), which is a scope choice to document honestly, not a claim that
Meta itself rejects named placeholders.
"""

import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import WhatsAppTemplate
from app.services.messaging.errors import MessagingError, MessagingErrorCode
from app.services.messaging.provider import TemplateComponent
from app.services.messaging.providers.whatsapp_cloud import RemoteTemplate

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
MAX_VARIABLES = 20
MAX_VARIABLE_LENGTH = 500
APPROVED_STATUS = "APPROVED"


def _body_text(components: list[dict[str, Any]]) -> str | None:
    for component in components:
        if isinstance(component, dict) and str(component.get("type", "")).upper() == "BODY":
            text = component.get("text")
            return text if isinstance(text, str) else None
    return None


def required_variable_keys(template: WhatsAppTemplate) -> list[str]:
    """The ordered positional keys ("1", "2", ...) the BODY component needs."""
    text = _body_text(template.components)
    if not text:
        return []
    names = _PLACEHOLDER.findall(text)
    if not names:
        return []
    if not all(n.isdigit() for n in names):
        raise MessagingError(
            MessagingErrorCode.INVALID_REQUEST,
            "template uses named placeholders, which is not supported",
        )
    ordered = sorted({int(n) for n in names})
    if ordered != list(range(1, len(ordered) + 1)):
        raise MessagingError(
            MessagingErrorCode.INVALID_REQUEST,
            "template placeholders must be sequential starting at {{1}}",
        )
    return [str(n) for n in ordered]


def build_body_component(
    template: WhatsAppTemplate, variables: dict[str, str]
) -> TemplateComponent | None:
    """Validate ``variables`` against the template and build the BODY component to send. Fails
    closed (``INVALID_REQUEST``) on any mismatch, missing key, or oversized value."""
    required = required_variable_keys(template)
    if not required:
        if variables:
            raise MessagingError(MessagingErrorCode.INVALID_REQUEST, "template takes no variables")
        return None
    if len(required) > MAX_VARIABLES:
        raise MessagingError(MessagingErrorCode.INVALID_REQUEST, "template has too many variables")
    if set(variables) != set(required):
        raise MessagingError(
            MessagingErrorCode.INVALID_REQUEST,
            "template variables do not match the template's placeholders",
        )
    for key in required:
        value = variables[key]
        if not isinstance(value, str) or not value or len(value) > MAX_VARIABLE_LENGTH:
            raise MessagingError(
                MessagingErrorCode.INVALID_REQUEST, "a template variable is invalid"
            )
    return TemplateComponent(
        type="body",
        parameters=tuple({"type": "text", "text": variables[key]} for key in required),
    )


def is_usable(template: WhatsAppTemplate) -> bool:
    return template.status.strip().upper() == APPROVED_STATUS


def get_template(
    db: Session,
    *,
    organization_id: uuid.UUID,
    connection_id: uuid.UUID,
    name: str,
    language: str,
) -> WhatsAppTemplate | None:
    return db.scalar(
        select(WhatsAppTemplate).where(
            WhatsAppTemplate.organization_id == organization_id,
            WhatsAppTemplate.connection_id == connection_id,
            WhatsAppTemplate.name == name,
            WhatsAppTemplate.language == language,
        )
    )


def sync_templates(
    db: Session,
    *,
    organization_id: uuid.UUID,
    connection_id: uuid.UUID,
    remote: list[RemoteTemplate],
    now: datetime,
) -> int:
    """Upsert templates from a live fetch. Callers commit. Never called with fabricated data: the
    caller must have actually reached Meta (see ``api/whatsapp/templates.py``)."""
    count = 0
    for item in remote:
        existing = get_template(
            db,
            organization_id=organization_id,
            connection_id=connection_id,
            name=item.name,
            language=item.language,
        )
        if existing is None:
            existing = WhatsAppTemplate(
                id=uuid.uuid4(),
                organization_id=organization_id,
                connection_id=connection_id,
                name=item.name,
                language=item.language,
                category=item.category,
                status=item.status,
            )
            db.add(existing)
        existing.category = item.category
        existing.status = item.status
        existing.provider_template_id = item.provider_template_id
        existing.components = item.components
        existing.last_synced_at = now
        count += 1
    return count
