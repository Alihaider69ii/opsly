"""Phase 10: D2C Operations AI API (brief section 28). Every route is tenant-scoped through
``CurrentOrg``/``ManagerOrg`` (see ``api/deps.py``) -- a row from another organization is always a
404, never a 403 (mirrors every other router in this codebase).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import CurrentOrg, DbSession, require_current_org_role
from app.db.models import (
    DetectedBy,
    IssueCategory,
    IssuePriority,
    IssueSeverity,
    IssueStatus,
    OperationalAction,
    OperationalIssue,
    OperationalTask,
    OperationsPrioritySource,
    OrganizationRole,
    TaskStatus,
)
from app.services.ai.errors import AIError
from app.services.ai.factory import get_llm_provider
from app.services.operations import (
    actions,
    ai,
    analytics,
    audit,
    issues,
    repository,
    service,
    tasks,
)
from app.services.organizations.service import OrgContext
from app.workers.queue import JobQueue, get_job_queue

router = APIRouter(prefix="/operations", tags=["operations"])
ManagerOrg = Annotated[
    OrgContext, Depends(require_current_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN))
]
Queue = Annotated[JobQueue, Depends(get_job_queue)]


# --- serialization --------------------------------------------------------------------------------


def _issue(row: OperationalIssue) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "customer_id": str(row.customer_id) if row.customer_id else None,
        "order_id": str(row.order_id) if row.order_id else None,
        "category": row.category.value,
        "subcategory": row.subcategory,
        "priority": row.priority.value,
        "priority_source": row.priority_source.value,
        "severity": row.severity.value,
        "status": row.status.value,
        "title": row.title,
        "description": row.description,
        "evidence": row.evidence,
        "detected_by": row.detected_by.value,
        "detector_code": row.detector_code,
        "recommended_action": row.recommended_action,
        "assigned_user_id": str(row.assigned_user_id) if row.assigned_user_id else None,
        "sla_id": str(row.sla_id) if row.sla_id else None,
        "due_at": row.due_at.isoformat() if row.due_at else None,
        "breached_at": row.breached_at.isoformat() if row.breached_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _task(row: OperationalTask) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "issue_id": str(row.issue_id) if row.issue_id else None,
        "customer_id": str(row.customer_id) if row.customer_id else None,
        "order_id": str(row.order_id) if row.order_id else None,
        "title": row.title,
        "description": row.description,
        "category": row.category.value,
        "priority": row.priority.value,
        "status": row.status.value,
        "assigned_user_id": str(row.assigned_user_id) if row.assigned_user_id else None,
        "created_by_type": row.created_by_type.value,
        "due_at": row.due_at.isoformat() if row.due_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "resolution": row.resolution,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _action(row: OperationalAction) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "action_type": row.action_type,
        "category": row.category.value,
        "resource_type": row.resource_type,
        "resource_id": str(row.resource_id) if row.resource_id else None,
        "status": row.status.value,
        "payload": row.payload,
        "result": row.result,
        "error_code": row.error_code,
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
        "created_at": row.created_at.isoformat(),
    }


def _audit_event(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "actor_type": row.actor_type.value,
        "actor_id": str(row.actor_id) if row.actor_id else None,
        "event_type": row.event_type,
        "resource_type": row.resource_type,
        "resource_id": str(row.resource_id) if row.resource_id else None,
        "metadata": row.metadata_,
        "created_at": row.created_at.isoformat(),
    }


def _get_issue_or_404(db: DbSession, ctx: OrgContext, issue_id: uuid.UUID) -> OperationalIssue:
    row = repository.get_issue(db, organization_id=ctx.organization.id, issue_id=issue_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Issue not found")
    return row


def _get_task_or_404(db: DbSession, ctx: OrgContext, task_id: uuid.UUID) -> OperationalTask:
    row = repository.get_task(db, organization_id=ctx.organization.id, task_id=task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    return row


def _get_action_or_404(db: DbSession, ctx: OrgContext, action_id: uuid.UUID) -> OperationalAction:
    row = repository.get_action(db, organization_id=ctx.organization.id, action_id=action_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
    return row


# --- request bodies -------------------------------------------------------------------------------


class IssueCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: IssueCategory
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    subcategory: str | None = Field(default=None, max_length=60)
    customer_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    priority: IssuePriority = IssuePriority.NORMAL
    severity: IssueSeverity = IssueSeverity.MEDIUM
    recommended_action: str | None = Field(default=None, max_length=1000)


class IssuePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priority: IssuePriority | None = None
    subcategory: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=2000)
    recommended_action: str | None = Field(default=None, max_length=1000)


class IssueAssign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assigned_user_id: uuid.UUID


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    category: IssueCategory
    description: str | None = Field(default=None, max_length=2000)
    issue_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    priority: IssuePriority = IssuePriority.NORMAL
    assigned_user_id: uuid.UUID | None = None


class TaskResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution: str | None = Field(default=None, max_length=1000)


class TaskCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=500)


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_type: str = Field(min_length=1, max_length=60)
    resource_type: str = Field(min_length=1, max_length=60)
    resource_id: uuid.UUID | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] | None = None
    reason: str | None = Field(default=None, max_length=500)


class AIQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)


# --- dashboard / analytics ------------------------------------------------------------------------


@router.get("/dashboard")
def dashboard(ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    org_id = ctx.organization.id
    urgent = repository.list_issues(
        db, organization_id=org_id, priority=IssuePriority.URGENT, limit=20
    )
    breaches = repository.list_issues(db, organization_id=org_id, breached_only=True, limit=20)
    return {
        "kpis": analytics.dashboard_kpis(db, organization_id=org_id),
        "urgent_queue": [_issue(row) for row in urgent],
        "sla_breaches": [_issue(row) for row in breaches],
        "orders_needing_attention": repository.list_orders_needing_attention(
            db, organization_id=org_id, limit=20
        ),
        "inventory_alerts": [
            row
            for row in repository.list_inventory_overview(db, organization_id=org_id, limit=100)
            if row["out_of_stock"] or row["low_stock"]
        ][:20],
        "open_tasks": [
            _task(row) for row in tasks.list_tasks(db, organization_id=org_id, limit=20)
        ],
        "recent_activity": [
            _audit_event(row) for row in audit.list_recent(db, organization_id=org_id, limit=20)
        ],
    }


@router.get("/analytics")
def get_analytics(ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    return analytics.full_analytics(db, organization_id=ctx.organization.id)


@router.post("/sweep")
def trigger_sweep(ctx: CurrentOrg, db: DbSession, queue: Queue) -> dict[str, Any]:
    """Run the detection sweep now (brief section 4). Also runs periodically from the worker."""
    result = service.run_detection_sweep(db, queue, organization_id=ctx.organization.id)
    return {
        "created": result.created,
        "updated": result.updated,
        "breached": result.breached,
        "total_detections": result.total_detections,
    }


# --- issues ---------------------------------------------------------------------------------------


@router.get("/issues")
def list_issues(
    ctx: CurrentOrg,
    db: DbSession,
    status_: Annotated[IssueStatus | None, Query(alias="status")] = None,
    priority: IssuePriority | None = None,
    category: IssueCategory | None = None,
    assigned_user_id: uuid.UUID | None = None,
    breached_only: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = repository.list_issues(
        db,
        organization_id=ctx.organization.id,
        status=status_,
        priority=priority,
        category=category,
        assigned_user_id=assigned_user_id,
        breached_only=breached_only,
        limit=limit,
    )
    return [_issue(row) for row in rows]


@router.post("/issues", status_code=status.HTTP_201_CREATED)
def create_issue_endpoint(body: IssueCreate, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    row = issues.create_issue(
        db,
        organization_id=ctx.organization.id,
        category=body.category,
        title=body.title,
        description=body.description,
        subcategory=body.subcategory,
        customer_id=body.customer_id,
        order_id=body.order_id,
        priority=body.priority,
        severity=body.severity,
        detected_by=DetectedBy.HUMAN,
        recommended_action=body.recommended_action,
        actor_id=ctx.user.id,
    )
    db.commit()
    db.refresh(row)
    return _issue(row)


@router.get("/issues/{issue_id}")
def get_issue_endpoint(issue_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    row = _get_issue_or_404(db, ctx, issue_id)
    result = _issue(row)
    result["audit"] = [
        _audit_event(event)
        for event in audit.list_for_resource(
            db,
            organization_id=ctx.organization.id,
            resource_type="operational_issue",
            resource_id=row.id,
        )
    ]
    result["tasks"] = [
        _task(task)
        for task in tasks.list_tasks(db, organization_id=ctx.organization.id, limit=50)
        if task.issue_id == row.id
    ]
    return result


@router.patch("/issues/{issue_id}")
def patch_issue(
    issue_id: uuid.UUID, body: IssuePatch, ctx: CurrentOrg, db: DbSession
) -> dict[str, Any]:
    row = _get_issue_or_404(db, ctx, issue_id)
    fields = body.model_fields_set
    if "priority" in fields and body.priority is not None:
        issues.set_priority(
            db,
            row,
            priority=body.priority,
            source=OperationsPrioritySource.HUMAN,
            actor_id=ctx.user.id,
        )
    if "subcategory" in fields:
        row.subcategory = body.subcategory
    if "description" in fields:
        row.description = body.description
    if "recommended_action" in fields:
        row.recommended_action = body.recommended_action
    db.commit()
    db.refresh(row)
    return _issue(row)


@router.post("/issues/{issue_id}/assign")
def assign_issue_endpoint(
    issue_id: uuid.UUID, body: IssueAssign, ctx: CurrentOrg, db: DbSession
) -> dict[str, Any]:
    row = _get_issue_or_404(db, ctx, issue_id)
    issues.assign_issue(db, row, assigned_user_id=body.assigned_user_id, actor_id=ctx.user.id)
    db.commit()
    db.refresh(row)
    return _issue(row)


@router.post("/issues/{issue_id}/resolve")
def resolve_issue_endpoint(issue_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    row = _get_issue_or_404(db, ctx, issue_id)
    issues.resolve_issue(db, row, actor_id=ctx.user.id)
    db.commit()
    db.refresh(row)
    return _issue(row)


@router.post("/issues/{issue_id}/reopen")
def reopen_issue_endpoint(issue_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    row = _get_issue_or_404(db, ctx, issue_id)
    issues.reopen_issue(db, row, actor_id=ctx.user.id)
    db.commit()
    db.refresh(row)
    return _issue(row)


@router.post("/issues/{issue_id}/dismiss")
def dismiss_issue_endpoint(issue_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    row = _get_issue_or_404(db, ctx, issue_id)
    issues.dismiss_issue(db, row, actor_id=ctx.user.id)
    db.commit()
    db.refresh(row)
    return _issue(row)


# --- tasks ----------------------------------------------------------------------------------------


@router.get("/tasks")
def list_tasks_endpoint(
    ctx: CurrentOrg,
    db: DbSession,
    status_: Annotated[TaskStatus | None, Query(alias="status")] = None,
    priority: IssuePriority | None = None,
    assigned_user_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = tasks.list_tasks(
        db,
        organization_id=ctx.organization.id,
        status=status_,
        priority=priority,
        assigned_user_id=assigned_user_id,
        limit=limit,
    )
    return [_task(row) for row in rows]


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
def create_task_endpoint(
    body: TaskCreate, ctx: CurrentOrg, db: DbSession, queue: Queue
) -> dict[str, Any]:
    row = tasks.create_task(
        db,
        organization_id=ctx.organization.id,
        title=body.title,
        category=body.category,
        created_by_type=DetectedBy.HUMAN,
        created_by_user_id=ctx.user.id,
        description=body.description,
        issue_id=body.issue_id,
        customer_id=body.customer_id,
        order_id=body.order_id,
        priority=body.priority,
        assigned_user_id=body.assigned_user_id,
    )
    db.commit()
    db.refresh(row)
    try:
        from app.services import automation

        automation.ingest_event(
            db,
            queue,
            organization_id=ctx.organization.id,
            event_type="operational_task_created",
            source="operations",
            source_id=str(row.id),
            dedupe_key=f"operations:operational_task_created:{row.id}",
            payload={"task": {"id": str(row.id)}, "category": row.category.value},
        )
        db.commit()
    except automation.InvalidWorkflow:
        db.rollback()
    return _task(row)


@router.get("/tasks/{task_id}")
def get_task_endpoint(task_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    return _task(_get_task_or_404(db, ctx, task_id))


@router.post("/tasks/{task_id}/claim")
def claim_task_endpoint(task_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    row = tasks.claim_task(
        db, organization_id=ctx.organization.id, task_id=task_id, user_id=ctx.user.id
    )
    if row is None:
        # Could be not-found, already claimed, or already resolved -- all a plain conflict here.
        existing = repository.get_task(db, organization_id=ctx.organization.id, task_id=task_id)
        if existing is None:
            db.rollback()
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Task is no longer open for claiming")
    db.commit()
    db.refresh(row)
    return _task(row)


@router.post("/tasks/{task_id}/release")
def release_task_endpoint(task_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    row = _get_task_or_404(db, ctx, task_id)
    tasks.release_task(db, row, actor_id=ctx.user.id)
    db.commit()
    db.refresh(row)
    return _task(row)


@router.post("/tasks/{task_id}/escalate")
def escalate_task_endpoint(task_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    row = _get_task_or_404(db, ctx, task_id)
    tasks.escalate_task(db, row, actor_id=ctx.user.id)
    db.commit()
    db.refresh(row)
    return _task(row)


@router.post("/tasks/{task_id}/resolve")
def resolve_task_endpoint(
    task_id: uuid.UUID, body: TaskResolve, ctx: CurrentOrg, db: DbSession
) -> dict[str, Any]:
    row = _get_task_or_404(db, ctx, task_id)
    tasks.resolve_task(db, row, resolution=body.resolution, actor_id=ctx.user.id)
    db.commit()
    db.refresh(row)
    return _task(row)


@router.post("/tasks/{task_id}/reopen")
def reopen_task_endpoint(task_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    row = _get_task_or_404(db, ctx, task_id)
    tasks.reopen_task(db, row, actor_id=ctx.user.id)
    db.commit()
    db.refresh(row)
    return _task(row)


@router.post("/tasks/{task_id}/cancel")
def cancel_task_endpoint(
    task_id: uuid.UUID, body: TaskCancel, ctx: CurrentOrg, db: DbSession
) -> dict[str, Any]:
    row = _get_task_or_404(db, ctx, task_id)
    tasks.cancel_task(db, row, actor_id=ctx.user.id, reason=body.reason)
    db.commit()
    db.refresh(row)
    return _task(row)


# --- order / inventory / shipping / payment / return views ----------------------------------------


@router.get("/orders")
def list_orders_endpoint(ctx: CurrentOrg, db: DbSession, limit: int = 100) -> list[dict[str, Any]]:
    return repository.list_orders_needing_attention(
        db, organization_id=ctx.organization.id, limit=limit
    )


@router.get("/inventory")
def list_inventory_endpoint(
    ctx: CurrentOrg, db: DbSession, limit: int = 100
) -> list[dict[str, Any]]:
    return repository.list_inventory_overview(db, organization_id=ctx.organization.id, limit=limit)


@router.get("/shipping")
def list_shipping_endpoint(
    ctx: CurrentOrg,
    db: DbSession,
    status_: Annotated[str | None, Query(alias="status")] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = repository.list_shipments(
        db, organization_id=ctx.organization.id, status=status_, limit=limit
    )
    return [
        {
            "id": str(row.id),
            "order_id": str(row.order_id),
            "carrier": row.carrier,
            "tracking_number": row.tracking_number,
            "status": row.status.value,
            "shipped_at": row.shipped_at.isoformat() if row.shipped_at else None,
            "estimated_delivery_at": row.estimated_delivery_at.isoformat()
            if row.estimated_delivery_at
            else None,
            "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
            "ndr_count": row.ndr_count,
        }
        for row in rows
    ]


@router.get("/payments")
def list_payments_endpoint(
    ctx: CurrentOrg, db: DbSession, limit: int = 100
) -> list[dict[str, Any]]:
    rows = repository.list_payment_orders(db, organization_id=ctx.organization.id, limit=limit)
    return [
        {
            "order_id": str(row.id),
            "order_number": row.order_number,
            "financial_status": row.financial_status,
            "total": str(row.total) if row.total is not None else None,
        }
        for row in rows
    ]


@router.get("/returns")
def list_returns_endpoint(
    ctx: CurrentOrg,
    db: DbSession,
    status_: Annotated[str | None, Query(alias="status")] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = repository.list_return_cases(
        db, organization_id=ctx.organization.id, status=status_, limit=limit
    )
    return [
        {
            "id": str(row.id),
            "order_id": str(row.order_id),
            "customer_id": str(row.customer_id) if row.customer_id else None,
            "type": row.type.value,
            "status": row.status.value,
            "requested_at": row.requested_at.isoformat(),
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        }
        for row in rows
    ]


# --- Operations AI -----------------------------------------------------------------------------


@router.post("/ai/query")
def ai_query(body: AIQuery, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    try:
        llm = get_llm_provider()
    except AIError:
        return {
            "summary": "The AI provider is not configured.",
            "evidence": [],
            "grounded": False,
        }
    result = ai.answer_query(db, llm, organization_id=ctx.organization.id, question=body.question)
    return {
        "summary": result.summary,
        "evidence": result.evidence,
        "reasoning_summary": result.reasoning_summary,
        "recommendation": result.recommendation,
        "risk": result.risk,
        "required_approval": result.required_approval,
        "suggested_action": result.suggested_action,
        "grounded": result.grounded,
    }


# --- safe action framework / approvals ----------------------------------------------------------


@router.get("/actions")
def list_actions_endpoint(ctx: CurrentOrg, db: DbSession) -> list[dict[str, Any]]:
    from sqlalchemy import select

    rows = db.scalars(
        select(OperationalAction)
        .where(OperationalAction.organization_id == ctx.organization.id)
        .order_by(OperationalAction.created_at.desc())
        .limit(100)
    )
    return [_action(row) for row in rows]


@router.post("/actions", status_code=status.HTTP_201_CREATED)
def request_action_endpoint(body: ActionRequest, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    row = actions.request_action(
        db,
        organization_id=ctx.organization.id,
        action_type=body.action_type,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        idempotency_key=body.idempotency_key,
        requested_by_type=DetectedBy.HUMAN,
        requested_by_user_id=ctx.user.id,
        payload=body.payload,
        reason=body.reason,
    )
    db.commit()
    db.refresh(row)
    return _action(row)


@router.get("/actions/{action_id}")
def get_action_endpoint(action_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    return _action(_get_action_or_404(db, ctx, action_id))


@router.post("/actions/{action_id}/approve")
def approve_action_endpoint(action_id: uuid.UUID, ctx: ManagerOrg, db: DbSession) -> dict[str, Any]:
    row = _get_action_or_404(db, ctx, action_id)
    try:
        actions.approve_action(db, row, approved_by=ctx.user.id)
    except actions.ActionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    db.commit()
    db.refresh(row)
    return _action(row)


@router.post("/actions/{action_id}/reject")
def reject_action_endpoint(action_id: uuid.UUID, ctx: ManagerOrg, db: DbSession) -> dict[str, Any]:
    row = _get_action_or_404(db, ctx, action_id)
    try:
        actions.reject_action(db, row, rejected_by=ctx.user.id)
    except actions.ActionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    db.commit()
    db.refresh(row)
    return _action(row)


@router.post("/actions/{action_id}/execute")
def execute_action_endpoint(action_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    row = _get_action_or_404(db, ctx, action_id)
    try:
        actions.execute_action(db, row)
    except actions.ActionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    db.commit()
    db.refresh(row)
    return _action(row)
