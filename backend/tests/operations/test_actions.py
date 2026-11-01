import uuid

from app.db.models import ActionCategory, ActionStatus, DetectedBy, IssueCategory, IssuePriority
from app.services.operations import actions, issues
from tests.operations.conftest import make_member


def test_classify_unknown_action_type_is_forbidden_by_construction():
    assert actions.classify("delete_entire_database") is ActionCategory.FORBIDDEN
    assert actions.classify("create_task") is ActionCategory.SAFE_AUTOMATION
    assert actions.classify("refund") is ActionCategory.REQUIRES_APPROVAL


def test_safe_action_is_auto_approved_and_executable_immediately(db, org_id):
    action = actions.request_action(
        db,
        organization_id=org_id,
        action_type="create_task",
        resource_type="operational_issue",
        resource_id=None,
        idempotency_key="k1",
        requested_by_type=DetectedBy.RULE,
        payload={"title": "do the thing", "category": "ORDER"},
    )
    db.commit()
    assert action.status is ActionStatus.APPROVED
    actions.execute_action(db, action)
    db.commit()
    assert action.status is ActionStatus.EXECUTED
    assert action.result["task_id"]


def test_forbidden_action_fails_immediately_and_never_executes(db, org_id):
    action = actions.request_action(
        db,
        organization_id=org_id,
        action_type="delete_everything",
        resource_type="order",
        resource_id=None,
        idempotency_key="k1",
        requested_by_type=DetectedBy.AI,
    )
    db.commit()
    assert action.status is ActionStatus.FAILED
    assert action.category is ActionCategory.FORBIDDEN


def test_requires_approval_action_blocks_until_approved(db, org_id, session_factory):
    action = actions.request_action(
        db,
        organization_id=org_id,
        action_type="refund",
        resource_type="order",
        resource_id=uuid.uuid4(),
        idempotency_key="k1",
        requested_by_type=DetectedBy.AI,
        payload={"amount": "50.00"},
    )
    db.commit()
    assert action.status is ActionStatus.PENDING
    assert action.approval_id is not None

    try:
        actions.execute_action(db, action)
        raised = False
    except actions.ActionError:
        raised = True
    assert raised, "an unapproved REQUIRES_APPROVAL action must never execute"

    approver = make_member(session_factory, org_id)
    actions.approve_action(db, action, approved_by=approver)
    db.commit()
    assert action.status is ActionStatus.APPROVED

    actions.execute_action(db, action)
    db.commit()
    assert action.status is ActionStatus.EXECUTED


def test_only_one_of_two_concurrent_approvals_succeeds(db, org_id, session_factory):
    """Two admins approving the same action at once (brief section 33's failure-injection list):
    exactly one logical approval. approvals.approve()'s atomic UPDATE...WHERE status=PENDING
    means the second caller's attempt matches zero rows once the first has committed, raising
    ApprovalNotPending (mapped by approve_action to ActionError) rather than silently succeeding
    twice -- the same race guard tasks.claim_task uses, exercised the same way: the first call
    commits, then the second call (simulating a racing request that read PENDING before the first
    committed) must fail against the now-committed row."""
    action = actions.request_action(
        db,
        organization_id=org_id,
        action_type="refund",
        resource_type="order",
        resource_id=uuid.uuid4(),
        idempotency_key="concurrent-approval-k1",
        requested_by_type=DetectedBy.AI,
        payload={"amount": "50.00"},
    )
    db.commit()

    admin_a = make_member(session_factory, org_id)
    admin_b = make_member(session_factory, org_id)

    actions.approve_action(db, action, approved_by=admin_a)
    db.commit()
    assert action.status is ActionStatus.APPROVED

    raised = False
    try:
        actions.approve_action(db, action, approved_by=admin_b)
    except actions.ActionError:
        raised = True
    assert raised, "a second approval of an already-approved action must be rejected, not silently accepted"

    db.refresh(action)
    assert action.status is ActionStatus.APPROVED  # unchanged by admin B's attempt


def test_rejected_action_never_executes(db, org_id, session_factory):
    action = actions.request_action(
        db,
        organization_id=org_id,
        action_type="cancel_order",
        resource_type="order",
        resource_id=uuid.uuid4(),
        idempotency_key="k1",
        requested_by_type=DetectedBy.AI,
    )
    db.commit()
    rejector = make_member(session_factory, org_id)
    actions.reject_action(db, action, rejected_by=rejector, reason="not warranted")
    db.commit()
    assert action.status is ActionStatus.REJECTED

    try:
        actions.execute_action(db, action)
        raised = False
    except actions.ActionError:
        raised = True
    assert raised


def test_request_action_is_idempotent_on_the_same_key(db, org_id):
    resource_id = uuid.uuid4()
    first = actions.request_action(
        db,
        organization_id=org_id,
        action_type="inventory_adjustment",
        resource_type="product_variant",
        resource_id=resource_id,
        idempotency_key="retry-1",
        requested_by_type=DetectedBy.AI,
        payload={"delta": -5},
    )
    db.commit()
    second = actions.request_action(
        db,
        organization_id=org_id,
        action_type="inventory_adjustment",
        resource_type="product_variant",
        resource_id=resource_id,
        idempotency_key="retry-1",
        requested_by_type=DetectedBy.AI,
        payload={"delta": -5},
    )
    db.commit()
    assert first.id == second.id


def test_execute_is_never_called_twice_for_the_same_action(db, org_id, session_factory):
    """A worker/webhook retry must not double-execute (brief section 21)."""
    resource_id = uuid.uuid4()
    action = actions.request_action(
        db,
        organization_id=org_id,
        action_type="refund",
        resource_type="order",
        resource_id=resource_id,
        idempotency_key="retry-exec",
        requested_by_type=DetectedBy.AI,
    )
    db.commit()
    approver = make_member(session_factory, org_id)
    actions.approve_action(db, action, approved_by=approver)
    db.commit()
    actions.execute_action(db, action)
    db.commit()
    assert action.status is ActionStatus.EXECUTED

    try:
        actions.execute_action(db, action)
        raised = False
    except actions.ActionError:
        raised = True
    assert raised, "re-executing an already-EXECUTED action must be refused, not repeated"


def test_change_priority_action_respects_human_priority_authority(db, org_id):
    issue = issues.create_issue(
        db,
        organization_id=org_id,
        category=IssueCategory.ORDER,
        title="x",
        detected_by=DetectedBy.HUMAN,
    )
    db.commit()

    action = actions.request_action(
        db,
        organization_id=org_id,
        action_type="change_priority",
        resource_type="operational_issue",
        resource_id=issue.id,
        idempotency_key="p1",
        requested_by_type=DetectedBy.AI,
        payload={"priority": IssuePriority.LOW.value},
    )
    db.commit()
    actions.execute_action(db, action)
    db.commit()
    assert action.status is ActionStatus.EXECUTED
    assert action.result["applied"] is False  # human priority stands
