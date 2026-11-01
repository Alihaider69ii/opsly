"""WhatsApp dashboard API schemas.

No access token, ciphertext or raw provider payload has a field here, so it cannot be serialized
by accident. Request bodies forbid unknown fields (no mass assignment).
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.db.models import (
    WhatsAppAIConfidence,
    WhatsAppAIState,
    WhatsAppConnectionStatus,
    WhatsAppConsentCategory,
    WhatsAppConsentStatus,
    WhatsAppConversationStatus,
    WhatsAppMessageDirection,
    WhatsAppMessageStatus,
    WhatsAppMessageType,
)


class WhatsAppConnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone_number_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
    ]
    waba_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    access_token: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    business_id: str | None = Field(default=None, max_length=64)
    # ISO 3166-1 alpha-2; required before any ambiguous (non-E.164) phone can be normalized.
    default_country: str | None = Field(default=None, min_length=2, max_length=2)


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    phone_number_id: str
    waba_id: str
    business_id: str | None
    display_phone_number: str | None
    verified_name: str | None
    quality_rating: str | None
    api_version: str
    default_country: str | None
    ai_auto_reply_enabled: bool
    ai_grounding_required: bool
    ai_min_confidence: WhatsAppAIConfidence
    ai_handoff_enabled: bool
    ai_fallback_message: str | None
    ai_handoff_message: str | None
    ai_max_replies_per_window: int
    status: WhatsAppConnectionStatus
    connected_at: datetime
    last_verified_at: datetime | None
    error_code: str | None
    error_message: str | None


class WhatsAppStatusOut(BaseModel):
    # Whether this server has Meta App credentials / can encrypt tokens.
    available: bool
    secure_storage: bool
    connection: ConnectionOut | None


class AutoReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class AISettingsIn(BaseModel):
    """Phase 8 AI conversation policy, organization-configurable (see docs/phase-8-progress.md
    "AI Settings"). Every field is required so a partial body can never silently leave another
    field at a stale value; the client always sends the full current settings back."""

    model_config = ConfigDict(extra="forbid")
    grounding_required: bool
    min_confidence: WhatsAppAIConfidence
    handoff_enabled: bool
    fallback_message: str | None = Field(default=None, max_length=1000)
    handoff_message: str | None = Field(default=None, max_length=1000)
    max_replies_per_window: int = Field(ge=0, le=1000)


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    contact_id: uuid.UUID
    connection_id: uuid.UUID
    status: WhatsAppConversationStatus
    last_inbound_at: datetime | None
    last_outbound_at: datetime | None
    window_expires_at: datetime | None
    message_count: int
    ai_state: WhatsAppAIState
    handoff_reason: str | None
    handoff_at: datetime | None
    ai_replies_in_window: int
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    direction: WhatsAppMessageDirection
    message_type: WhatsAppMessageType
    status: WhatsAppMessageStatus
    content: str | None
    template_name: str | None
    template_language: str | None
    error_code: str | None
    # Populated only for Phase 8 AI-generated outbound messages; see
    # app.db.models.whatsapp.WhatsAppMessage.ai_metadata / .ai_policy.
    ai: dict[str, Any] | None = Field(validation_alias="ai_metadata", default=None)
    policy: dict[str, Any] | None = Field(validation_alias="ai_policy", default=None)
    created_at: datetime
    sent_at: datetime | None
    delivered_at: datetime | None
    read_at: datetime | None
    failed_at: datetime | None


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    language: str
    category: str
    status: str
    provider_template_id: str | None
    components: list[dict[str, Any]]
    last_synced_at: datetime | None


class TemplateSyncOut(BaseModel):
    synced: int


class ConsentOut(BaseModel):
    category: WhatsAppConsentCategory
    status: WhatsAppConsentStatus | None  # None = UNKNOWN (no explicit record on file)


class ConsentListOut(BaseModel):
    contact_id: uuid.UUID
    consents: list[ConsentOut]
    suppressed: bool


class ConsentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: WhatsAppConsentCategory
    status: WhatsAppConsentStatus
    source: str = Field(min_length=1, max_length=60)


class OptOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="user_requested", min_length=1, max_length=60)


class AutomationUsageOut(BaseModel):
    workflow_id: uuid.UUID
    name: str
    status: str
    whatsapp_actions: list[str]
