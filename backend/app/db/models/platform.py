"""Platform-level identity/access audit (brief section 16), separate from
``OperationalAuditEvent`` because it records a different concern: who accessed or changed *which
brand's existence/membership*, not what happened inside one brand's operations. A platform admin
acting inside a brand workspace is recorded here as itself -- never as a synthetic brand user
(brief section 13).
"""

import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class PlatformAuditEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "platform_audit_events"
    __table_args__ = (
        Index("ix_platform_audit_events_org_created", "organization_id", "created_at"),
    )

    # The platform admin who acted -- always a real user id at write time (there is no synthetic
    # actor), but nullable because the FK is ondelete=SET NULL: this row, like the organization_id
    # one below, must survive the referenced row being deleted later rather than vanishing with it.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[str] = mapped_column(index=True)
    # Nullable: some events (e.g. PLATFORM_ADMIN_LOGIN) have no single brand they're about.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="SET NULL"), index=True
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
