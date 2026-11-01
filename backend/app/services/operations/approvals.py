"""The approval system (brief section 20). Used internally by ``actions.py`` -- the public API
exposes only action-level endpoints (``/operations/actions/{id}/approve|reject|execute``, brief
section 28), never a separate approvals endpoint, so every approval is always reached through the
action it gates. "Only authorized users can approve" is enforced by the API layer's role
dependency (an ADMIN/OWNER-only route, mirroring ``api/automations.py:ManagerOrg``), not here.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.models import ApprovalStatus, DetectedBy, OperationalApproval


class ApprovalNotPending(ValueError):
    pass


def request_approval(
    *,
    organization_id: uuid.UUID,
    action_type: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    requested_by_type: DetectedBy,
    requested_by: uuid.UUID | None = None,
    reason: str | None = None,
    payload: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> OperationalApproval:
    return OperationalApproval(
        id=uuid.uuid4(),
        organization_id=organization_id,
        action_type=action_type,
        resource_type=resource_type,
        resource_id=resource_id,
        requested_by_type=requested_by_type,
        requested_by=requested_by,
        status=ApprovalStatus.PENDING,
        reason=reason,
        payload=payload,
        expires_at=expires_at,
    )


def approve(
    db: Session,
    approval: OperationalApproval,
    *,
    approved_by: uuid.UUID,
    now: datetime | None = None,
) -> OperationalApproval:
    """Atomic ``UPDATE ... WHERE status = PENDING`` (the same race guard as
    ``tasks.claim_task``): two admins approving the same action at once must never both succeed
    (brief section 33, "concurrent approval: exactly one logical approval"). Raises
    ApprovalNotPending if the row was not PENDING at the moment of the update -- including the
    case where another request's approval/rejection won the race in between this caller's read
    and write.
    """
    result = db.execute(
        update(OperationalApproval)
        .where(
            OperationalApproval.id == approval.id,
            OperationalApproval.status == ApprovalStatus.PENDING,
        )
        .values(
            status=ApprovalStatus.APPROVED,
            approved_by=approved_by,
            approved_at=now or datetime.now(UTC),
        )
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        db.refresh(approval)
        raise ApprovalNotPending(f"approval is {approval.status.value}, not PENDING")
    db.refresh(approval)
    return approval


def reject(
    db: Session,
    approval: OperationalApproval,
    *,
    rejected_by: uuid.UUID,
    reason: str | None = None,
    now: datetime | None = None,
) -> OperationalApproval:
    """Same atomic race guard as approve() -- see its docstring."""
    result = db.execute(
        update(OperationalApproval)
        .where(
            OperationalApproval.id == approval.id,
            OperationalApproval.status == ApprovalStatus.PENDING,
        )
        .values(
            status=ApprovalStatus.REJECTED,
            rejected_by=rejected_by,
            rejected_at=now or datetime.now(UTC),
            **({"reason": reason} if reason else {}),
        )
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        db.refresh(approval)
        raise ApprovalNotPending(f"approval is {approval.status.value}, not PENDING")
    db.refresh(approval)
    return approval


def is_expired(approval: OperationalApproval, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    if approval.expires_at is None:
        return False
    expires_at = (
        approval.expires_at
        if approval.expires_at.tzinfo
        else approval.expires_at.replace(tzinfo=UTC)
    )
    return expires_at <= now
