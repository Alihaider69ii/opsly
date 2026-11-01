"""Unified support inbox dashboard API schemas (Phase 9).

No access token, ciphertext or raw provider payload has a field here. Request bodies forbid
unknown fields (no mass assignment).
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import (
    AIHandoffState,
    SenderType,
    SupportChannel,
    SupportPriority,
    SupportStatus,
)


class CustomerSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    shopify_customer_id: int | None
    email_marketing_consent: bool | None
    email_suppressed: bool


class ConversationSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel: SupportChannel
    channel_conversation_id: uuid.UUID
    customer_id: uuid.UUID | None
    status: SupportStatus
    priority: SupportPriority
    ai_state: AIHandoffState
    assigned_agent_user_id: uuid.UUID | None
    last_customer_activity_at: datetime | None
    last_response_at: datetime | None
    last_response_sender_type: SenderType | None
    last_message_preview: str | None
    unread: bool
    created_at: datetime
    updated_at: datetime
    customer: CustomerSummaryOut | None = None


class MessageOut(BaseModel):
    id: uuid.UUID
    direction: str
    sender_type: SenderType | None
    sender_user_id: uuid.UUID | None
    content: str | None
    status: str
    ai: dict[str, Any] | None
    policy: dict[str, Any] | None
    error_code: str | None
    created_at: datetime


class TimelineEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    support_conversation_id: uuid.UUID | None
    created_at: datetime


class ReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=5000)


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_user_id: uuid.UUID


class PriorityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: SupportPriority
    reason: str | None = Field(default=None, max_length=200)


class RequestHumanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="dashboard_requested", min_length=1, max_length=60)


class ActionResult(BaseModel):
    ok: bool
    conversation: ConversationSummaryOut


class AnalyticsOut(BaseModel):
    total_conversations: int
    open_conversations: int
    urgent_conversations: int
    high_priority_conversations: int
    ai_handled: int
    human_handled: int
    ai_handoffs: int
    avg_first_response_seconds: float | None
    avg_resolution_seconds: float | None
    emails_sent: int
    emails_delivered: int
    emails_bounced: int
    emails_unsubscribed: int
