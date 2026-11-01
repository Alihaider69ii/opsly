import enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.organization_member import OrganizationMember


class OrganizationEnvironment(enum.StrEnum):
    """DEMO vs PRODUCTION (brief section 9) -- an explicit field, never inferred from the name,
    so demo data can never be mistaken for a real customer's."""

    DEMO = "DEMO"
    PRODUCTION = "PRODUCTION"


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The tenant. Every business-owned table references this via organization_id."""

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    website: Mapped[str | None] = mapped_column(String(2048))
    business_type: Mapped[str | None] = mapped_column(String(100))
    environment: Mapped[OrganizationEnvironment] = mapped_column(
        Enum(
            OrganizationEnvironment,
            native_enum=False,
            length=20,
            create_constraint=True,
            name="organization_environment",
        ),
        default=OrganizationEnvironment.PRODUCTION,
        server_default=OrganizationEnvironment.PRODUCTION.value,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    members: Mapped[list["OrganizationMember"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
