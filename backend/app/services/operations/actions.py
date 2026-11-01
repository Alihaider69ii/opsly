"""The safe action framework (brief section 19) + its idempotency ledger (brief section 21).

Every externally/financially meaningful operation goes through ``request_action`` ->
(``approve_action``/``reject_action`` if REQUIRES_APPROVAL) -> ``execute_action``. The category
(SAFE_AUTOMATION / REQUIRES_APPROVAL / FORBIDDEN) is resolved ONCE, from the static
``ACTION_POLICY`` map below, at request time -- never by the caller, and never by the AI
(brief section 34: "AI output is advisory until accepted/executed by the action layer").
``execute_action`` only ever runs after ``status == APPROVED``, so a FORBIDDEN or still-PENDING
action can never execute.

Destructive actions without a live external integration (no Shopify order-mutation API is wired up
in Phase 10 -- see ``docs/operations-architecture.md``) still go through the full
request/approve/execute/audit pipeline; ``_record_only`` makes that explicit in the result rather
than pretending something happened externally.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    ActionCategory,
    ActionStatus,
    ApprovalStatus,
    DetectedBy,
    IssuePriority,
    OperationalAction,
    OperationalApproval,
    OperationalIssue,
    OperationsPrioritySource,
    ProductVariant,
    ReturnCase,
    ReturnCaseStatus,
)
from app.services.operations import approvals, audit, issues, tasks

MAX_PAYLOAD_CHARS = 4000

# Resolved once, deterministically, at request time (brief section 19). Anything not listed here
# is FORBIDDEN by construction -- there is no implicit default-allow.
ACTION_POLICY: dict[str, ActionCategory] = {
    "create_task": ActionCategory.SAFE_AUTOMATION,
    "assign_task": ActionCategory.SAFE_AUTOMATION,
    "change_priority": ActionCategory.SAFE_AUTOMATION,
    "send_internal_notification": ActionCategory.SAFE_AUTOMATION,
    "create_support_followup": ActionCategory.SAFE_AUTOMATION,
    "refund": ActionCategory.REQUIRES_APPROVAL,
    "cancel_order": ActionCategory.REQUIRES_APPROVAL,
    "modify_order": ActionCategory.REQUIRES_APPROVAL,
    "change_customer_address": ActionCategory.REQUIRES_APPROVAL,
    "inventory_adjustment": ActionCategory.REQUIRES_APPROVAL,
    "customer_compensation": ActionCategory.REQUIRES_APPROVAL,
}


def classify(action_type: str) -> ActionCategory:
    return ACTION_POLICY.get(action_type, ActionCategory.FORBIDDEN)


class ActionError(ValueError):
    pass


def _bounded(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    return payload if len(str(payload)) <= MAX_PAYLOAD_CHARS else {"truncated": True}


def request_action(
    db: Session,
    *,
    organization_id: uuid.UUID,
    action_type: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    idempotency_key: str,
    requested_by_type: DetectedBy,
    requested_by_user_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    reason: str | None = None,
) -> OperationalAction:
    """Idempotent: a repeated call with the same (action_type, resource_type, resource_id,
    idempotency_key) returns the existing row untouched instead of requesting/executing again
    (brief section 21 -- webhook retries, worker retries, AI retries must never double-act)."""
    existing = db.scalar(
        select(OperationalAction).where(
            OperationalAction.organization_id == organization_id,
            OperationalAction.action_type == action_type,
            OperationalAction.resource_type == resource_type,
            OperationalAction.resource_id == resource_id,
            OperationalAction.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing

    category = classify(action_type)
    action = OperationalAction(
        id=uuid.uuid4(),
        organization_id=organization_id,
        action_type=action_type[:60],
        category=category,
        resource_type=resource_type[:60],
        resource_id=resource_id,
        idempotency_key=idempotency_key[:200],
        requested_by_type=requested_by_type,
        requested_by_user_id=requested_by_user_id,
        payload=_bounded(payload),
        status=ActionStatus.PENDING,
    )
    db.add(action)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return request_action(
            db,
            organization_id=organization_id,
            action_type=action_type,
            resource_type=resource_type,
            resource_id=resource_id,
            idempotency_key=idempotency_key,
            requested_by_type=requested_by_type,
            requested_by_user_id=requested_by_user_id,
            payload=payload,
            reason=reason,
        )
    audit.record(
        db,
        organization_id=organization_id,
        actor_type=requested_by_type,
        actor_id=requested_by_user_id,
        event_type="ACTION_REQUESTED",
        resource_type="operational_action",
        resource_id=action.id,
        metadata={"action_type": action_type, "category": category.value},
    )
    if category is ActionCategory.FORBIDDEN:
        action.status = ActionStatus.FAILED
        action.error_code = "forbidden_action_type"
        audit.record(
            db,
            organization_id=organization_id,
            actor_type=requested_by_type,
            event_type="ACTION_FAILED",
            resource_type="operational_action",
            resource_id=action.id,
            metadata={"error_code": action.error_code},
        )
    elif category is ActionCategory.REQUIRES_APPROVAL:
        approval = approvals.request_approval(
            organization_id=organization_id,
            action_type=action_type,
            resource_type=resource_type,
            resource_id=resource_id,
            requested_by_type=requested_by_type,
            requested_by=requested_by_user_id,
            reason=reason,
            payload=payload,
        )
        db.add(approval)
        db.flush()
        action.approval_id = approval.id
    else:
        action.status = ActionStatus.APPROVED  # SAFE_AUTOMATION needs no human gate
    db.flush()
    return action


def approve_action(
    db: Session, action: OperationalAction, *, approved_by: uuid.UUID
) -> OperationalAction:
    if action.category is not ActionCategory.REQUIRES_APPROVAL or action.approval_id is None:
        raise ActionError("action does not require approval")
    approval = db.get(OperationalApproval, action.approval_id)
    if approval is None:
        raise ActionError("linked approval not found")
    try:
        approvals.approve(db, approval, approved_by=approved_by)
    except approvals.ApprovalNotPending as exc:
        # Covers both a plain re-approve and losing a concurrent-approval race (brief section 33):
        # either way the caller gets the same 409 the router already maps ActionError to.
        raise ActionError(str(exc)) from exc
    action.status = ActionStatus.APPROVED
    audit.record(
        db,
        organization_id=action.organization_id,
        actor_type=DetectedBy.HUMAN,
        actor_id=approved_by,
        event_type="ACTION_APPROVED",
        resource_type="operational_action",
        resource_id=action.id,
    )
    return action


def reject_action(
    db: Session, action: OperationalAction, *, rejected_by: uuid.UUID, reason: str | None = None
) -> OperationalAction:
    if action.category is not ActionCategory.REQUIRES_APPROVAL or action.approval_id is None:
        raise ActionError("action does not require approval")
    approval = db.get(OperationalApproval, action.approval_id)
    if approval is None:
        raise ActionError("linked approval not found")
    try:
        approvals.reject(db, approval, rejected_by=rejected_by, reason=reason)
    except approvals.ApprovalNotPending as exc:
        raise ActionError(str(exc)) from exc
    action.status = ActionStatus.REJECTED
    audit.record(
        db,
        organization_id=action.organization_id,
        actor_type=DetectedBy.HUMAN,
        actor_id=rejected_by,
        event_type="ACTION_REJECTED",
        resource_type="operational_action",
        resource_id=action.id,
        metadata={"reason": reason},
    )
    return action


class Executor(Protocol):
    def __call__(self, db: Session, action: OperationalAction) -> dict[str, Any]: ...


def _record_only(db: Session, action: OperationalAction) -> dict[str, Any]:
    return {
        "note": (
            "No live external integration executes this action type in Phase 10; recorded as "
            "approved and executed in BizFlow's own ledger only."
        ),
        "action_type": action.action_type,
    }


def _exec_create_task(db: Session, action: OperationalAction) -> dict[str, Any]:
    payload = action.payload or {}
    issue_id = uuid.UUID(payload["issue_id"]) if payload.get("issue_id") else None
    task = tasks.create_task(
        db,
        organization_id=action.organization_id,
        title=payload.get("title", "Follow up")[:300],
        category=issues_category_from_payload(action, payload),
        created_by_type=action.requested_by_type,
        created_by_user_id=action.requested_by_user_id,
        issue_id=issue_id,
    )
    return {"task_id": str(task.id)}


def issues_category_from_payload(action: OperationalAction, payload: dict[str, Any]):
    from app.db.models import IssueCategory

    raw = payload.get("category")
    try:
        return IssueCategory(raw) if raw else IssueCategory.OTHER
    except ValueError:
        return IssueCategory.OTHER


def _exec_assign_task(db: Session, action: OperationalAction) -> dict[str, Any]:
    from app.db.models import OperationalTask

    payload = action.payload or {}
    task = db.get(OperationalTask, action.resource_id)
    if task is None or task.organization_id != action.organization_id:
        raise ActionError("task not found")
    assigned_user_id = uuid.UUID(payload["assigned_user_id"])
    tasks.assign_task(
        db, task, assigned_user_id=assigned_user_id, actor_id=action.requested_by_user_id
    )
    return {"task_id": str(task.id), "assigned_user_id": str(assigned_user_id)}


def _exec_change_priority(db: Session, action: OperationalAction) -> dict[str, Any]:
    payload = action.payload or {}
    issue = db.get(OperationalIssue, action.resource_id)
    if issue is None or issue.organization_id != action.organization_id:
        raise ActionError("issue not found")
    priority = IssuePriority(payload["priority"])
    source = (
        OperationsPrioritySource.HUMAN
        if action.requested_by_type is DetectedBy.HUMAN
        else OperationsPrioritySource.AI
    )
    applied = issues.set_priority(db, issue, priority=priority, source=source)
    return {"issue_id": str(issue.id), "priority": priority.value, "applied": applied}


def _exec_notify(db: Session, action: OperationalAction) -> dict[str, Any]:
    # No internal notification channel exists yet in Phase 10 -- recorded in the audit log, which
    # is itself the internal-visibility mechanism the Operations dashboard reads from.
    payload = action.payload or {}
    return {"notified": True, "message": str(payload.get("message", ""))[:500]}


def _exec_inventory_adjustment(db: Session, action: OperationalAction) -> dict[str, Any]:
    payload = action.payload or {}
    variant = db.get(ProductVariant, action.resource_id)
    if variant is None or variant.organization_id != action.organization_id:
        raise ActionError("product variant not found")
    delta = int(payload["delta"])
    current = variant.inventory_quantity or 0
    variant.inventory_quantity = max(0, current + delta)
    return {
        "variant_id": str(variant.id),
        "delta": delta,
        "new_quantity": variant.inventory_quantity,
    }


def _exec_refund(db: Session, action: OperationalAction) -> dict[str, Any]:
    if action.resource_type == "return_case":
        case = db.get(ReturnCase, action.resource_id)
        if case is None or case.organization_id != action.organization_id:
            raise ActionError("return case not found")
        case.status = ReturnCaseStatus.REFUNDED
        case.resolved_at = datetime.now(UTC)
        return {"return_case_id": str(case.id), "status": case.status.value}
    return _record_only(db, action)


_EXECUTORS: dict[str, Executor] = {
    "create_task": _exec_create_task,
    "create_support_followup": _exec_create_task,
    "assign_task": _exec_assign_task,
    "change_priority": _exec_change_priority,
    "send_internal_notification": _exec_notify,
    "inventory_adjustment": _exec_inventory_adjustment,
    "refund": _exec_refund,
}


def execute_action(db: Session, action: OperationalAction) -> OperationalAction:
    if action.status is not ActionStatus.APPROVED:
        raise ActionError(f"action is {action.status.value}, not APPROVED")
    executor = _EXECUTORS.get(action.action_type, _record_only)
    try:
        result = executor(db, action)
        action.status = ActionStatus.EXECUTED
        action.result = _bounded(result)
        action.executed_at = datetime.now(UTC)
        audit.record(
            db,
            organization_id=action.organization_id,
            actor_type=action.requested_by_type,
            actor_id=action.requested_by_user_id,
            event_type="ACTION_EXECUTED",
            resource_type="operational_action",
            resource_id=action.id,
            metadata={"action_type": action.action_type},
        )
    except ActionError as exc:
        action.status = ActionStatus.FAILED
        action.error_code = str(exc)[:60]
        audit.record(
            db,
            organization_id=action.organization_id,
            actor_type=action.requested_by_type,
            event_type="ACTION_FAILED",
            resource_type="operational_action",
            resource_id=action.id,
            metadata={"error_code": action.error_code},
        )
    return action


def is_approval_expired_due(approval: OperationalApproval, *, now: datetime | None = None) -> bool:
    return approval.status is ApprovalStatus.PENDING and approvals.is_expired(approval, now=now)
