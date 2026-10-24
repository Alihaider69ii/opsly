import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    HttpUrl,
    StringConstraints,
    field_validator,
)

from app.db.models.organization_member import OrganizationRole

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
MIN_SLUG_LENGTH = 3
MAX_SLUG_LENGTH = 50


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _normalize_slug(value: Any) -> Any:
    value = _blank_to_none(value)
    return value.lower() if isinstance(value, str) else value


# Constraints sit on the str branch only; they must not be applied to None.
Slug = Annotated[
    str,
    StringConstraints(pattern=SLUG_PATTERN, min_length=MIN_SLUG_LENGTH, max_length=MAX_SLUG_LENGTH),
]
BusinessType = Annotated[str, StringConstraints(max_length=100)]
OptionalSlug = Annotated[Slug | None, BeforeValidator(_normalize_slug)]
OptionalBusinessType = Annotated[BusinessType | None, BeforeValidator(_blank_to_none)]
OptionalUrl = Annotated[HttpUrl | None, BeforeValidator(_blank_to_none)]


class OrganizationCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    slug: OptionalSlug = None  # generated from the name when omitted
    website: OptionalUrl = None
    business_type: OptionalBusinessType = None


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    website: str | None
    business_type: str | None
    created_at: datetime


class OrganizationWithRole(OrganizationOut):
    """An organization as seen by one member: includes that member's role."""

    role: OrganizationRole


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str | None
    role: OrganizationRole
    joined_at: datetime


class MemberAdd(BaseModel):
    """Add an already-registered user. Invitation flows (email) come in a later phase."""

    email: EmailStr
    role: OrganizationRole = OrganizationRole.MEMBER

    @field_validator("role")
    @classmethod
    def _owner_not_grantable(cls, v: OrganizationRole) -> OrganizationRole:
        if v == OrganizationRole.OWNER:
            raise ValueError("The OWNER role cannot be granted")
        return v
