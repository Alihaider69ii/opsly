"""Operational issue lifecycle (brief section 2). Every mutation is a deterministic function here
-- the AI layer (``ai.py``) and detectors (``detectors.py``) only ever call into this module; they
never touch ``OperationalIssue`` rows directly (brief sections 2, 34).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    DetectedBy,
    IssueCategory,
    IssuePriority,
    IssueSeverity,
    IssueStatus,
    OperationalIssue,
    OperationsPrioritySource,
    RiskLevel,
)
from app.services.operations import audit
from app.services.operations.priorities import PriorityResult, compute_priority
from app.services.operations.sla import (
    ISSUE_CATEGORY_TO_SLA_CATEGORY,
    ensure_default_slas,
    evaluate,
    select_sla,
)

MAX_EVIDENCE_CHARS = 6000


@dataclass(frozen=True)
class Detection:
    """What a detector (or the support bridge) found. Passed to ``upsert_from_detection``; never
    constructed from unvalidated external input."""

    organization_id: uuid.UUID
    category: IssueCategory
    detector_code: str
    dedupe_key: str
    title: str
    description: str | None = None
    subcategory: str | None = None
    evidence: dict[str, Any] | None = None
    customer_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    recommended_action: str | None = None
    severity: IssueSeverity = IssueSeverity.MEDIUM
    order_value: Decimal | None = None
    repeated_incidents: int = 0
    risk_level: RiskLevel | None = None
    detected_by: DetectedBy = DetectedBy.RULE


def _bounded_evidence(evidence: dict[str, Any] | None) -> dict[str, Any]:
    evidence = evidence or {}
    if len(str(evidence)) > MAX_EVIDENCE_CHARS:
        return {"truncated": True}
    return evidence


def _priority_for(detection: Detection) -> PriorityResult:
    return compute_priority(
        order_value=detection.order_value,
        sla_breached=False,
        repeated_incidents=detection.repeated_incidents,
        risk_level=detection.risk_level,
        severity=detection.severity,
    )


def apply_sla(db: Session, issue: OperationalIssue, *, now: datetime | None = None) -> bool:
    """Recompute ``due_at``/``breached_at`` from the current category/priority/creation time.
    Returns True the moment the issue transitions into breach (so the caller can emit the
    ``sla_breached`` automation event / audit entry exactly once)."""
    now = now or datetime.now(UTC)
    sla_category = ISSUE_CATEGORY_TO_SLA_CATEGORY[issue.category]
    sla = select_sla(
        db, organization_id=issue.organization_id, category=sla_category, priority=issue.priority
    )
    if sla is None:
        # The default SLA catalog is normally seeded by the first detection sweep (service.py), but
        # an issue can be created before that ever runs (manual API create, support bridge, a brand
        # new organization) -- never leave due_at unset just because of ordering.
        ensure_default_slas(db, issue.organization_id)
        sla = select_sla(
            db,
            organization_id=issue.organization_id,
            category=sla_category,
            priority=issue.priority,
        )
    issue.sla_id = sla.id if sla else None
    result = evaluate(sla, started_at=issue.created_at, now=now)
    issue.due_at = result.due_at
    newly_breached = False
    if (
        result.breached
        and issue.breached_at is None
        and issue.status
        not in (
            IssueStatus.RESOLVED,
            IssueStatus.DISMISSED,
        )
    ):
        issue.breached_at = now
        newly_breached = True
    elif not result.breached:
        issue.breached_at = None
    return newly_breached


def upsert_from_detection(
    db: Session, detection: Detection, *, now: datetime | None = None
) -> tuple[OperationalIssue, bool, bool]:
    """Idempotent: re-running a detector sweep updates the existing issue for the same root cause
    instead of duplicating it (``dedupe_key`` is unique per organization). A resolved/dismissed
    issue whose root cause fires again is reopened rather than silently left closed.

    Returns ``(issue, created, newly_breached)``.
    """
    now = now or datetime.now(UTC)
    evidence = _bounded_evidence(detection.evidence)
    existing = db.scalar(
        select(OperationalIssue).where(
            OperationalIssue.organization_id == detection.organization_id,
            OperationalIssue.dedupe_key == detection.dedupe_key,
        )
    )
    priority_result = _priority_for(detection)
    if existing is not None:
        existing.evidence = evidence
        existing.description = detection.description
        existing.recommended_action = detection.recommended_action
        existing.subcategory = detection.subcategory
        reopened = existing.status in (IssueStatus.RESOLVED, IssueStatus.DISMISSED)
        if reopened:
            existing.status = IssueStatus.OPEN
            existing.resolved_at = None
        # A human's priority stands until a human changes it (brief section 6).
        if existing.priority_source != OperationsPrioritySource.HUMAN:
            existing.priority = priority_result.priority
            existing.priority_source = OperationsPrioritySource.SYSTEM
            existing.severity = detection.severity
        newly_breached = apply_sla(db, existing, now=now)
        db.flush()
        if reopened:
            audit.record(
                db,
                organization_id=detection.organization_id,
                actor_type=DetectedBy.RULE,
                event_type="ISSUE_REOPENED",
                resource_type="operational_issue",
                resource_id=existing.id,
                metadata={"detector_code": detection.detector_code},
            )
        return existing, False, newly_breached

    issue = OperationalIssue(
        id=uuid.uuid4(),
        organization_id=detection.organization_id,
        customer_id=detection.customer_id,
        order_id=detection.order_id,
        category=detection.category,
        subcategory=detection.subcategory,
        priority=priority_result.priority,
        priority_source=OperationsPrioritySource.SYSTEM,
        severity=detection.severity,
        status=IssueStatus.OPEN,
        title=detection.title[:300],
        description=detection.description,
        evidence=evidence,
        detected_by=detection.detected_by,
        detector_code=detection.detector_code,
        recommended_action=detection.recommended_action,
        dedupe_key=detection.dedupe_key,
        created_at=now,
    )
    db.add(issue)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return upsert_from_detection(db, detection, now=now)
    newly_breached = apply_sla(db, issue, now=now)
    audit.record(
        db,
        organization_id=detection.organization_id,
        actor_type=detection.detected_by,
        event_type="ISSUE_CREATED",
        resource_type="operational_issue",
        resource_id=issue.id,
        metadata={"detector_code": detection.detector_code, "category": detection.category.value},
    )
    db.flush()
    return issue, True, newly_breached


def create_issue(
    db: Session,
    *,
    organization_id: uuid.UUID,
    category: IssueCategory,
    title: str,
    description: str | None = None,
    subcategory: str | None = None,
    customer_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
    priority: IssuePriority = IssuePriority.NORMAL,
    severity: IssueSeverity = IssueSeverity.MEDIUM,
    detected_by: DetectedBy = DetectedBy.HUMAN,
    recommended_action: str | None = None,
    evidence: dict[str, Any] | None = None,
    actor_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> OperationalIssue:
    now = now or datetime.now(UTC)
    issue = OperationalIssue(
        id=uuid.uuid4(),
        organization_id=organization_id,
        customer_id=customer_id,
        order_id=order_id,
        category=category,
        subcategory=subcategory,
        priority=priority,
        priority_source=(
            OperationsPrioritySource.HUMAN
            if detected_by == DetectedBy.HUMAN
            else OperationsPrioritySource.SYSTEM
        ),
        severity=severity,
        status=IssueStatus.OPEN,
        title=title[:300],
        description=description,
        evidence=_bounded_evidence(evidence),
        detected_by=detected_by,
        recommended_action=recommended_action,
        created_at=now,
    )
    db.add(issue)
    db.flush()
    apply_sla(db, issue, now=now)
    audit.record(
        db,
        organization_id=organization_id,
        actor_type=detected_by,
        actor_id=actor_id,
        event_type="ISSUE_CREATED",
        resource_type="operational_issue",
        resource_id=issue.id,
        metadata={"category": category.value, "manual": True},
    )
    db.flush()
    return issue


def set_priority(
    db: Session,
    issue: OperationalIssue,
    *,
    priority: IssuePriority,
    source: OperationsPrioritySource,
    actor_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> bool:
    """Returns False (no-op) when a non-human tries to override a human-set priority."""
    if (
        issue.priority_source == OperationsPrioritySource.HUMAN
        and source != OperationsPrioritySource.HUMAN
    ):
        return False
    if issue.priority == priority and issue.priority_source == source:
        return True
    issue.priority = priority
    issue.priority_source = source
    apply_sla(db, issue)
    audit.record(
        db,
        organization_id=issue.organization_id,
        actor_type=DetectedBy.HUMAN if source == OperationsPrioritySource.HUMAN else DetectedBy.AI,
        actor_id=actor_id,
        event_type="ISSUE_PRIORITY_CHANGED",
        resource_type="operational_issue",
        resource_id=issue.id,
        metadata={"priority": priority.value, "source": source.value, "reason": reason},
    )
    return True


def assign_issue(
    db: Session,
    issue: OperationalIssue,
    *,
    assigned_user_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
) -> OperationalIssue:
    issue.assigned_user_id = assigned_user_id
    if issue.status == IssueStatus.OPEN:
        issue.status = IssueStatus.ACKNOWLEDGED
    audit.record(
        db,
        organization_id=issue.organization_id,
        actor_type=DetectedBy.HUMAN,
        actor_id=actor_id,
        event_type="ISSUE_ASSIGNED",
        resource_type="operational_issue",
        resource_id=issue.id,
        metadata={"assigned_user_id": str(assigned_user_id)},
    )
    return issue


def resolve_issue(
    db: Session,
    issue: OperationalIssue,
    *,
    actor_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> OperationalIssue:
    now = now or datetime.now(UTC)
    issue.status = IssueStatus.RESOLVED
    issue.resolved_at = now
    audit.record(
        db,
        organization_id=issue.organization_id,
        actor_type=DetectedBy.HUMAN,
        actor_id=actor_id,
        event_type="ISSUE_RESOLVED",
        resource_type="operational_issue",
        resource_id=issue.id,
    )
    return issue


def reopen_issue(
    db: Session, issue: OperationalIssue, *, actor_id: uuid.UUID | None = None
) -> OperationalIssue:
    issue.status = IssueStatus.OPEN
    issue.resolved_at = None
    apply_sla(db, issue)
    audit.record(
        db,
        organization_id=issue.organization_id,
        actor_type=DetectedBy.HUMAN,
        actor_id=actor_id,
        event_type="ISSUE_REOPENED",
        resource_type="operational_issue",
        resource_id=issue.id,
    )
    return issue


def dismiss_issue(
    db: Session,
    issue: OperationalIssue,
    *,
    reason: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> OperationalIssue:
    issue.status = IssueStatus.DISMISSED
    audit.record(
        db,
        organization_id=issue.organization_id,
        actor_type=DetectedBy.HUMAN,
        actor_id=actor_id,
        event_type="ISSUE_DISMISSED",
        resource_type="operational_issue",
        resource_id=issue.id,
        metadata={"reason": reason},
    )
    return issue
