from datetime import timedelta
from decimal import Decimal

from app.db.models import (
    DetectedBy,
    IssueCategory,
    IssuePriority,
    IssueStatus,
    OperationsPrioritySource,
)
from app.services.operations import issues
from tests.operations.conftest import NOW, make_customer, make_order


def _detection(org_id, **overrides):
    defaults = dict(
        organization_id=org_id,
        category=IssueCategory.ORDER,
        detector_code="ORDER_UNFULFILLED_SLA",
        dedupe_key="ORDER_UNFULFILLED_SLA:order-1",
        title="Order unfulfilled",
    )
    defaults.update(overrides)
    return issues.Detection(**defaults)


def test_upsert_creates_a_new_issue_once(db, org_id):
    issue, created, breached = issues.upsert_from_detection(db, _detection(org_id), now=NOW)
    db.commit()
    assert created is True
    assert issue.status is IssueStatus.OPEN
    assert issue.detected_by is DetectedBy.RULE


def test_upsert_is_idempotent_on_dedupe_key(db, org_id):
    issues.upsert_from_detection(db, _detection(org_id), now=NOW)
    db.commit()
    issue2, created2, _ = issues.upsert_from_detection(db, _detection(org_id), now=NOW)
    db.commit()
    assert created2 is False

    from sqlalchemy import func, select

    from app.db.models import OperationalIssue

    count = db.scalar(
        select(func.count())
        .select_from(OperationalIssue)
        .where(OperationalIssue.organization_id == org_id)
    )
    assert count == 1


def test_resolved_issue_is_reopened_when_the_same_root_cause_recurs(db, org_id):
    issue, _, _ = issues.upsert_from_detection(db, _detection(org_id), now=NOW)
    db.commit()
    issues.resolve_issue(db, issue, now=NOW)
    db.commit()
    assert issue.status is IssueStatus.RESOLVED

    reopened, created, _ = issues.upsert_from_detection(
        db, _detection(org_id), now=NOW + timedelta(hours=1)
    )
    db.commit()
    assert created is False
    assert reopened.id == issue.id
    assert reopened.status is IssueStatus.OPEN
    assert reopened.resolved_at is None


def test_human_set_priority_is_never_overridden_by_a_later_detection(db, org_id):
    issue, _, _ = issues.upsert_from_detection(db, _detection(org_id), now=NOW)
    db.commit()
    issues.set_priority(
        db, issue, priority=IssuePriority.LOW, source=OperationsPrioritySource.HUMAN, actor_id=None
    )
    db.commit()
    assert issue.priority is IssuePriority.LOW

    # Same root cause fires again with a high-value order, which would normally push priority up.
    updated, _, _ = issues.upsert_from_detection(
        db, _detection(org_id, order_value=Decimal("99999")), now=NOW + timedelta(hours=1)
    )
    db.commit()
    assert updated.priority is IssuePriority.LOW
    assert updated.priority_source is OperationsPrioritySource.HUMAN


def test_ai_or_system_cannot_silently_override_a_human_priority(db, org_id):
    issue, _, _ = issues.upsert_from_detection(db, _detection(org_id), now=NOW)
    db.commit()
    issues.set_priority(
        db, issue, priority=IssuePriority.URGENT, source=OperationsPrioritySource.HUMAN
    )
    db.commit()
    applied = issues.set_priority(
        db, issue, priority=IssuePriority.LOW, source=OperationsPrioritySource.AI
    )
    assert applied is False
    assert issue.priority is IssuePriority.URGENT


def test_apply_sla_sets_due_at_and_flags_breach_exactly_once(db, org_id):
    issue, _, _ = issues.upsert_from_detection(db, _detection(org_id), now=NOW)
    db.commit()
    assert issue.due_at is not None
    assert issue.breached_at is None

    far_future = issue.due_at + timedelta(days=10)
    assert issues.apply_sla(db, issue, now=far_future) is True
    db.commit()
    assert issue.breached_at is not None
    first_breach_time = issue.breached_at

    # Calling apply_sla again after breach must not reset breached_at (section 6: a breach, once
    # recorded, is not silently un-recorded by a later recompute at the same priority).
    issues.apply_sla(db, issue, now=far_future + timedelta(hours=1))
    db.commit()
    assert issue.breached_at == first_breach_time


def test_create_issue_links_customer_and_order(db, org_id):
    customer = make_customer(db, org_id)
    order = make_order(db, org_id, customer=customer, shopify_order_id=42)
    db.commit()
    issue = issues.create_issue(
        db,
        organization_id=org_id,
        category=IssueCategory.ORDER,
        title="Manual issue",
        customer_id=customer.id,
        order_id=order.id,
        detected_by=DetectedBy.HUMAN,
    )
    db.commit()
    assert issue.customer_id == customer.id
    assert issue.order_id == order.id
    assert issue.priority_source is OperationsPrioritySource.HUMAN
