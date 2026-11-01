"""Orchestration: the detection sweep that ties detectors -> issue engine -> automation triggers
-> audit together (brief sections 4, 26). This is the one place detector output turns into
persisted issues and, for the specific event types named in the brief, an automation event.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import DetectedBy
from app.services import automation
from app.services.operations import audit, issues, sla
from app.services.operations.detectors import run_all
from app.workers.queue import JobQueue

# Maps a detector_code prefix to the automation trigger it fires (brief section 26's exact list).
# Categories with no listed trigger (PAYMENT, RETURN, EXCHANGE, WAREHOUSE) intentionally emit none.
_EVENT_TYPE_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("SHIPMENT_DELAYED", "shipment_delayed"),
    ("NDR", "ndr_detected"),
    ("RTO", "rto_detected"),
    ("INVENTORY_", "inventory_low"),
    ("REFUND_PENDING", "refund_pending"),
    ("ORDER_", "order_needs_attention"),
)


def _automation_event_type(detector_code: str | None) -> str | None:
    if not detector_code:
        return None
    for prefix, event_type in _EVENT_TYPE_BY_PREFIX:
        if detector_code.startswith(prefix):
            return event_type
    return None


@dataclass
class SweepResult:
    created: int = 0
    updated: int = 0
    breached: int = 0
    total_detections: int = 0


def _emit(
    db: Session,
    queue: JobQueue,
    *,
    organization_id: uuid.UUID,
    event_type: str,
    issue_id: uuid.UUID,
    payload: dict,
    now: datetime,
) -> None:
    try:
        automation.ingest_event(
            db,
            queue,
            organization_id=organization_id,
            event_type=event_type,
            source="operations",
            source_id=str(issue_id),
            dedupe_key=f"operations:{event_type}:{issue_id}",
            payload=payload,
            occurred_at=now,
        )
    except automation.InvalidWorkflow:
        # No automation workflow subscribes to this event type, or the payload shape changed --
        # the operational issue itself is already persisted and visible on the dashboard either way.
        pass


def run_detection_sweep(
    db: Session, queue: JobQueue, *, organization_id: uuid.UUID, now: datetime | None = None
) -> SweepResult:
    now = now or datetime.now(UTC)
    sla.ensure_default_slas(db, organization_id)
    result = SweepResult()
    for detection in run_all(db, organization_id, now=now):
        result.total_detections += 1
        issue, created, newly_breached = issues.upsert_from_detection(db, detection, now=now)
        if created:
            result.created += 1
            event_type = _automation_event_type(detection.detector_code)
            if event_type:
                _emit(
                    db,
                    queue,
                    organization_id=organization_id,
                    event_type=event_type,
                    issue_id=issue.id,
                    payload={
                        "issue": {"id": str(issue.id)},
                        "order": {"id": str(issue.order_id)} if issue.order_id else {},
                        "customer": {"id": str(issue.customer_id)} if issue.customer_id else {},
                        "category": issue.category.value,
                        "priority": issue.priority.value,
                    },
                    now=now,
                )
        else:
            result.updated += 1
        if newly_breached:
            result.breached += 1
            audit.record(
                db,
                organization_id=organization_id,
                actor_type=DetectedBy.RULE,
                event_type="SLA_BREACHED",
                resource_type="operational_issue",
                resource_id=issue.id,
                metadata={"category": issue.category.value, "priority": issue.priority.value},
            )
            _emit(
                db,
                queue,
                organization_id=organization_id,
                event_type="sla_breached",
                issue_id=issue.id,
                payload={"issue": {"id": str(issue.id)}, "category": issue.category.value},
                now=now,
            )
        db.commit()
    return result
