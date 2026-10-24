"""Tenant-scoped workflow management and execution inspection."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.api.deps import CurrentOrg, DbSession, require_current_org_role
from app.db.models import (
    AutomationExecution,
    AutomationExecutionStep,
    AutomationWorkflow,
    OrganizationRole,
    WorkflowStatus,
)
from app.services.automation import InvalidWorkflow, validate_definition
from app.services.organizations.service import OrgContext

router = APIRouter(prefix="/automations", tags=["automations"])
ManagerOrg = Annotated[
    OrgContext, Depends(require_current_org_role(OrganizationRole.OWNER, OrganizationRole.ADMIN))
]


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    definition: dict[str, Any]


class WorkflowPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    definition: dict[str, Any] | None = None


def _workflow(row: AutomationWorkflow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "status": row.status.value,
        "version": row.version,
        "definition": row.definition,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _execution(row: AutomationExecution) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "workflow_id": str(row.workflow_id),
        "event_id": str(row.event_id),
        "workflow_version": row.workflow_version,
        "status": row.status.value,
        "current_step": row.current_step,
        "attempts": row.attempts,
        "error_code": row.error_code,
        "created_at": row.created_at.isoformat(),
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


@router.get("")
def list_workflows(ctx: CurrentOrg, db: DbSession) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(AutomationWorkflow)
            .where(AutomationWorkflow.organization_id == ctx.organization.id)
            .order_by(AutomationWorkflow.updated_at.desc())
            .limit(200)
        )
    )
    output = []
    workflow_ids = [row.id for row in rows]
    recent_by_workflow: dict[uuid.UUID, list[AutomationExecution]] = {
        workflow_id: [] for workflow_id in workflow_ids
    }
    if workflow_ids:
        ranked = (
            select(
                AutomationExecution,
                func.row_number()
                .over(
                    partition_by=AutomationExecution.workflow_id,
                    order_by=AutomationExecution.created_at.desc(),
                )
                .label("run_number"),
            )
            .where(
                AutomationExecution.organization_id == ctx.organization.id,
                AutomationExecution.workflow_id.in_(workflow_ids),
            )
            .subquery()
        )
        executions = db.scalars(
            select(AutomationExecution)
            .join(ranked, ranked.c.id == AutomationExecution.id)
            .where(ranked.c.run_number <= 10)
            .order_by(AutomationExecution.created_at.desc())
        )
        for run in executions:
            recent_by_workflow[run.workflow_id].append(run)
    for row in rows:
        result = _workflow(row)
        result["recent_executions"] = [_execution(run) for run in recent_by_workflow[row.id]]
        output.append(result)
    return output


@router.post("", status_code=status.HTTP_201_CREATED)
def create_workflow(body: WorkflowCreate, ctx: ManagerOrg, db: DbSession) -> dict[str, Any]:
    try:
        definition = validate_definition(body.definition)
    except InvalidWorkflow as exc:
        raise HTTPException(422, str(exc)) from None
    row = AutomationWorkflow(
        organization_id=ctx.organization.id,
        name=body.name.strip(),
        description=body.description,
        status=WorkflowStatus.DRAFT,
        version=1,
        definition=definition,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _workflow(row)


@router.get("/{workflow_id}")
def get_workflow(workflow_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    row = db.scalar(
        select(AutomationWorkflow).where(
            AutomationWorkflow.id == workflow_id,
            AutomationWorkflow.organization_id == ctx.organization.id,
        )
    )
    if row is None:
        raise HTTPException(404, "Workflow not found")
    result = _workflow(row)
    result["recent_executions"] = [
        _execution(item)
        for item in db.scalars(
            select(AutomationExecution)
            .where(
                AutomationExecution.workflow_id == row.id,
                AutomationExecution.organization_id == ctx.organization.id,
            )
            .order_by(AutomationExecution.created_at.desc())
            .limit(25)
        )
    ]
    return result


@router.patch("/{workflow_id}")
def update_workflow(
    workflow_id: uuid.UUID, body: WorkflowPatch, ctx: ManagerOrg, db: DbSession
) -> dict[str, Any]:
    row = db.scalar(
        select(AutomationWorkflow)
        .where(
            AutomationWorkflow.id == workflow_id,
            AutomationWorkflow.organization_id == ctx.organization.id,
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Workflow not found")
    if row.status == WorkflowStatus.ARCHIVED:
        raise HTTPException(409, "Archived workflows cannot be edited")
    fields = body.model_fields_set
    if "name" in fields and body.name is not None:
        row.name = body.name.strip()
    if "description" in fields:
        row.description = body.description
    if "definition" in fields and body.definition is not None:
        try:
            row.definition = validate_definition(body.definition)
        except InvalidWorkflow as exc:
            raise HTTPException(422, str(exc)) from None
        row.version += 1
    db.commit()
    db.refresh(row)
    return _workflow(row)


def _status(
    workflow_id: uuid.UUID, status_: WorkflowStatus, ctx: OrgContext, db: Any
) -> dict[str, Any]:
    row = db.scalar(
        select(AutomationWorkflow)
        .where(
            AutomationWorkflow.id == workflow_id,
            AutomationWorkflow.organization_id == ctx.organization.id,
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Workflow not found")
    if row.status == WorkflowStatus.ARCHIVED:
        raise HTTPException(409, "Archived workflow cannot change status")
    row.status = status_
    db.commit()
    db.refresh(row)
    return _workflow(row)


@router.post("/{workflow_id}/activate")
def activate(workflow_id: uuid.UUID, ctx: ManagerOrg, db: DbSession) -> dict[str, Any]:
    return _status(workflow_id, WorkflowStatus.ACTIVE, ctx, db)


@router.post("/{workflow_id}/pause")
def pause(workflow_id: uuid.UUID, ctx: ManagerOrg, db: DbSession) -> dict[str, Any]:
    return _status(workflow_id, WorkflowStatus.PAUSED, ctx, db)


@router.post("/{workflow_id}/archive")
def archive(workflow_id: uuid.UUID, ctx: ManagerOrg, db: DbSession) -> dict[str, Any]:
    return _status(workflow_id, WorkflowStatus.ARCHIVED, ctx, db)


@router.get("/{workflow_id}/executions")
def executions(
    workflow_id: uuid.UUID, ctx: CurrentOrg, db: DbSession, limit: int = 50
) -> list[dict[str, Any]]:
    if not db.scalar(
        select(AutomationWorkflow.id).where(
            AutomationWorkflow.id == workflow_id,
            AutomationWorkflow.organization_id == ctx.organization.id,
        )
    ):
        raise HTTPException(404, "Workflow not found")
    return [
        _execution(row)
        for row in db.scalars(
            select(AutomationExecution)
            .where(
                AutomationExecution.workflow_id == workflow_id,
                AutomationExecution.organization_id == ctx.organization.id,
            )
            .order_by(AutomationExecution.created_at.desc())
            .limit(max(1, min(limit, 100)))
        )
    ]


@router.get("/executions/{execution_id}")
def execution_detail(execution_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:
    row = db.scalar(
        select(AutomationExecution).where(
            AutomationExecution.id == execution_id,
            AutomationExecution.organization_id == ctx.organization.id,
        )
    )
    if row is None:
        raise HTTPException(404, "Execution not found")
    result = _execution(row)
    result["workflow_snapshot"] = row.workflow_snapshot
    result["steps"] = [
        {
            "index": step.step_index,
            "action": step.action_snapshot,
            "status": step.status.value,
            "attempts": step.attempts,
            "error_code": step.error_code,
            "result": step.safe_result,
            "scheduled_at": step.scheduled_at.isoformat() if step.scheduled_at else None,
        }
        for step in db.scalars(
            select(AutomationExecutionStep)
            .where(
                AutomationExecutionStep.execution_id == row.id,
                AutomationExecutionStep.organization_id == ctx.organization.id,
            )
            .order_by(AutomationExecutionStep.step_index)
        )
    ]
    return result
