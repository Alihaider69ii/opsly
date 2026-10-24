from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.organization_member import OrganizationMember


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The tenant. Every business-owned table references this via organization_id."""

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    website: Mapped[str | None] = mapped_column(String(2048))
    business_type: Mapped[str | None] = mapped_column(String(100))

    members: Mapped[list["OrganizationMember"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
