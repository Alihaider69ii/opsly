import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

from app.db.models import (
    AutomationEvent,
    AutomationExecution,
    AutomationWorkflow,
    ExecutionStatus,
    Organization,
    WorkflowStatus,
)
from app.services import automation
from app.workers.queue import AutomationEventJob, AutomationExecutionJob


def _org(db, label):
    row = Organization(
        name=f"Automation {label}", slug=f"automation-{label}-{uuid.uuid4().hex[:8]}"
    )
    db.add(row)
    db.flush()
    return row


def test_postgres_event_dispatch_execution_and_redis_delivery(pg, redis_queue):
    organization = _org(pg, "flow")
    workflow = AutomationWorkflow(
        organization_id=organization.id,
        name="Order follow-up",
        status=WorkflowStatus.ACTIVE,
        version=1,
        definition={
            "trigger": {"event": "order_created"},
            "conditions": [],
            "actions": [{"type": "create_task", "title": "Review new order"}],
        },
    )
    pg.add(workflow)
    pg.commit()
    event = automation.ingest_event(
        pg,
        redis_queue,
        organization_id=organization.id,
        event_type="order_created",
        source="shopify",
        source_id="order-77",
        dedupe_key="shopify:order_created:77",
        payload={"order": {"total": "199.00"}},
    )
    assert isinstance(redis_queue.dequeue(timeout=0), AutomationEventJob)
    assert automation.dispatch_event(pg, event.id, redis_queue) == 1
    job = redis_queue.dequeue(timeout=0)
    assert isinstance(job, AutomationExecutionJob)
    assert automation.run_execution(pg, job.execution_id, redis_queue) == "completed"
    execution = pg.get(AutomationExecution, job.execution_id)
    assert execution is not None and execution.status == ExecutionStatus.COMPLETED
    assert automation.run_execution(pg, execution.id, redis_queue) == "skipped"


def test_postgres_composite_fks_reject_cross_tenant_execution_references(pg):
    first, second = _org(pg, "first"), _org(pg, "second")
    workflow = AutomationWorkflow(
        organization_id=first.id,
        name="First tenant",
        status=WorkflowStatus.ACTIVE,
        version=1,
        definition={
            "trigger": {"event": "order_created"},
            "conditions": [],
            "actions": [{"type": "log", "message": "audit"}],
        },
    )
    event = AutomationEvent(
        organization_id=second.id,
        event_type="order_created",
        source="internal",
        dedupe_key="second-event",
        occurred_at=datetime.now(UTC),
        payload={},
        depth=0,
        created_at=datetime.now(UTC),
    )
    pg.add_all([workflow, event])
    pg.commit()
    pg.add(
        AutomationExecution(
            organization_id=first.id,
            workflow_id=workflow.id,
            event_id=event.id,
            workflow_version=1,
            workflow_snapshot={"definition": workflow.definition},
            event_snapshot={},
            status=ExecutionStatus.QUEUED,
            current_step=0,
            attempts=0,
            max_attempts=5,
            created_at=datetime.now(UTC),
        )
    )
    try:
        pg.commit()
    except IntegrityError:
        pg.rollback()
    else:
        raise AssertionError("cross-tenant event reference was accepted")
