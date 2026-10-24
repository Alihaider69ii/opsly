import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator, model_validator

_ORIGIN_RE = re.compile(r"^https?://[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:\d{1,5})?$")
MAX_ALLOWED_ORIGINS = 20

# Hard schema-level ceiling; the configurable per-message limit is enforced in the service.
RawMessage = Annotated[str, StringConstraints(max_length=10_000)]
VisitorId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,64}$")]


class ConversationCreate(BaseModel):
    visitor_id: VisitorId | None = None


class ConversationCreated(BaseModel):
    conversation_id: uuid.UUID
    # Shown once. Sent back as X-Conversation-Token to prove ownership of the conversation.
    token: str


class MessageCreate(BaseModel):
    conversation_id: uuid.UUID
    content: RawMessage


class LeadCapture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: uuid.UUID
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)] | None = None
    email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=320)] | None = None
    phone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)] | None = None
    marketing_consent: bool = False

    @model_validator(mode="after")
    def _contact_required(self) -> "LeadCapture":
        if not self.email and not self.phone:
            raise ValueError("Provide an email address or phone number")
        return self

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.lower()
            if (
                value.count("@") != 1
                or "." not in value.rsplit("@", 1)[-1]
                or any(c.isspace() for c in value)
            ):
                raise ValueError("Enter a valid email address")
        return value

    @field_validator("phone")
    @classmethod
    def _phone_shape(cls, value: str | None) -> str | None:
        if value is not None:
            digits = sum(char.isdigit() for char in value)
            if digits < 7 or len(value) > 50:
                raise ValueError("Enter a valid phone number")
        return value


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class ChatReply(BaseModel):
    conversation_id: uuid.UUID
    message: MessageOut


class ConversationOut(BaseModel):
    conversation_id: uuid.UUID
    status: Literal["OPEN", "CLOSED"]
    messages: list[MessageOut]


class WidgetPublicConfig(BaseModel):
    """What the embedded widget may learn about a business: its name and greeting. Nothing else."""

    business_name: str
    welcome_message: str


class WidgetOut(BaseModel):
    """Dashboard view of the organization's widget settings."""

    public_key: str
    enabled: bool
    welcome_message: str
    allowed_origins: list[str]


class WidgetUpdate(BaseModel):
    enabled: bool | None = None
    welcome_message: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None
    ) = None
    allowed_origins: list[str] | None = None

    @field_validator("allowed_origins")
    @classmethod
    def _normalize_origins(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) > MAX_ALLOWED_ORIGINS:
            raise ValueError(f"At most {MAX_ALLOWED_ORIGINS} origins are allowed")
        cleaned: list[str] = []
        for item in value:
            origin = item.strip().rstrip("/").lower()
            if not origin:
                continue
            if not _ORIGIN_RE.match(origin):
                raise ValueError(f"'{item}' is not a valid origin (use https://example.com)")
            if origin not in cleaned:
                cleaned.append(origin)
        return cleaned
