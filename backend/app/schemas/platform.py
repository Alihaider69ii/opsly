import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints

from app.db.models.organization import OrganizationEnvironment
from app.schemas.auth import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from app.schemas.organization import OptionalSlug

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class BrandCreate(BaseModel):
    name: Name
    slug: OptionalSlug = None
    environment: OrganizationEnvironment = OrganizationEnvironment.PRODUCTION
    owner_name: Name
    owner_email: EmailStr
    temporary_password: Annotated[
        str, StringConstraints(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    ]


class BrandOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    environment: OrganizationEnvironment
    is_active: bool
    owner_email: str | None
    user_count: int
    customer_count: int
    open_issues: int
    open_tasks: int
    created_at: datetime
    last_activity_at: datetime | None


class BrandCreated(BaseModel):
    """Never includes the password -- the caller already has it (they just set it)."""

    brand: BrandOut
    owner_email: str


class PlatformDashboard(BaseModel):
    total_brands: int
    active_brands: int
    demo_brands: int
    production_brands: int
    total_users: int
    total_customers: int
    total_conversations: int
    open_support_issues: int
    open_operational_issues: int
    open_operational_tasks: int
    ai_queries_recent: int
    connected_integrations: int


class PlatformAuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    actor_email: str | None
    event_type: str
    organization_id: uuid.UUID | None
    organization_name: str | None
    metadata: dict[str, Any]
    created_at: datetime


class EnterBrandResult(BaseModel):
    organization: BrandOut
