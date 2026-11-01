"""Email dashboard API schemas (Phase 9). No access token or raw provider payload has a field
here. Request bodies forbid unknown fields (no mass assignment)."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.db.models import (
    EmailCampaignStatus,
    EmailClassification,
    EmailMessageStatus,
    EmailTemplateStatus,
    WhatsAppAIConfidence,
    WhatsAppConnectionStatus,
)

Email = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=320)]


class EmailConnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_address: Email
    from_name: str | None = Field(default=None, max_length=255)
    reply_to_address: Email | None = None


class EmailConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: WhatsAppConnectionStatus
    from_address: str
    from_name: str | None
    reply_to_address: str | None
    inbound_address: str
    enabled: bool
    ai_auto_reply_enabled: bool
    ai_grounding_required: bool
    ai_min_confidence: WhatsAppAIConfidence
    ai_handoff_enabled: bool
    ai_fallback_message: str | None
    ai_handoff_message: str | None
    ai_max_replies_per_window: int
    connected_at: datetime
    error_code: str | None
    error_message: str | None


class EmailStatusOut(BaseModel):
    available: bool
    connection: EmailConnectionOut | None


class EmailAutoReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class EmailAISettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grounding_required: bool
    min_confidence: WhatsAppAIConfidence
    handoff_enabled: bool
    fallback_message: str | None = Field(default=None, max_length=1000)
    handoff_message: str | None = Field(default=None, max_length=1000)
    max_replies_per_window: int = Field(ge=0, le=1000)


class EmailTemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    type: EmailClassification
    subject: str = Field(min_length=1, max_length=500)
    preheader: str | None = Field(default=None, max_length=200)
    body_html: str = Field(min_length=1, max_length=100_000)
    body_text: str | None = Field(default=None, max_length=100_000)
    variables: list[str] = Field(default_factory=list, max_length=50)


class EmailTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    type: EmailClassification
    subject: str
    preheader: str | None
    body_html: str
    body_text: str | None
    variables: list[str]
    status: EmailTemplateStatus
    version: int
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class EmailTemplateStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: EmailTemplateStatus


class EmailCampaignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    template_id: uuid.UUID
    audience_definition: dict[str, Any] = Field(default_factory=dict)
    scheduled_at: datetime | None = None


class EmailCampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    template_id: uuid.UUID
    audience_definition: dict[str, Any]
    status: EmailCampaignStatus
    scheduled_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_by: uuid.UUID | None
    total_recipients: int
    sent_count: int
    failed_count: int
    skipped_count: int
    created_at: datetime
    updated_at: datetime


class EmailCampaignActionOut(BaseModel):
    campaign: EmailCampaignOut


class AutomationUsageOut(BaseModel):
    workflow_id: uuid.UUID
    name: str
    status: str
    email_actions: list[str]


class EmailMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    thread_id: uuid.UUID
    direction: str
    status: EmailMessageStatus
    from_address: str
    to_addresses: list[str]
    subject: str | None
    body_text: str | None
    body_html_sanitized: str | None
    classification: EmailClassification | None
    error_code: str | None
    created_at: datetime
    sent_at: datetime | None
    delivered_at: datetime | None
