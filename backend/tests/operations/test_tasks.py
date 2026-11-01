import uuid

from app.db.models import DetectedBy, IssueCategory, TaskStatus
from app.services.operations import tasks
from tests.operations.conftest import make_member


def _make_task(db, org_id):
    task = tasks.create_task(
        db,
        organization_id=org_id,
        title="Follow up",
        category=IssueCategory.ORDER,
        created_by_type=DetectedBy.RULE,
    )
    db.commit()
    return task


def test_claim_succeeds_on_an_open_unassigned_task(db, org_id, session_factory):
    task = _make_task(db, org_id)
    user_id = make_member(session_factory, org_id)

    claimed = tasks.claim_task(db, organization_id=org_id, task_id=task.id, user_id=user_id)
    db.commit()
    assert claimed is not None
    assert claimed.assigned_user_id == user_id
    assert claimed.status is TaskStatus.ASSIGNED


def test_only_one_of_two_concurrent_claims_succeeds(db, org_id, session_factory):
    """Two agents claiming the same task: only one succeeds (brief section 3). The atomic
    UPDATE...WHERE means the second caller's claim attempt simply matches zero rows once the
    first has committed -- this exercises that exact code path."""
    task = _make_task(db, org_id)
    user_a = make_member(session_factory, org_id)
    user_b = make_member(session_factory, org_id)

    first = tasks.claim_task(db, organization_id=org_id, task_id=task.id, user_id=user_a)
    db.commit()
    second = tasks.claim_task(db, organization_id=org_id, task_id=task.id, user_id=user_b)
    db.commit()

    assert first is not None
    assert second is None
    assert task.assigned_user_id == user_a


def test_claim_fails_for_a_nonexistent_task(db, org_id, session_factory):
    user_id = make_member(session_factory, org_id)
    result = tasks.claim_task(db, organization_id=org_id, task_id=uuid.uuid4(), user_id=user_id)
    assert result is None


def test_claim_fails_cross_tenant(db, org_id, session_factory):
    task = _make_task(db, org_id)
    other_org = uuid.uuid4()
    from app.db.models import Organization

    with session_factory() as other_db:
        other_db.add(Organization(id=other_org, name="Other", slug=f"other-{other_org.hex[:8]}"))
        other_db.commit()
    user_id = make_member(session_factory, other_org)

    result = tasks.claim_task(db, organization_id=other_org, task_id=task.id, user_id=user_id)
    assert result is None


def test_release_then_reclaim(db, org_id, session_factory):
    task = _make_task(db, org_id)
    user_a = make_member(session_factory, org_id)
    user_b = make_member(session_factory, org_id)

    tasks.claim_task(db, organization_id=org_id, task_id=task.id, user_id=user_a)
    db.commit()
    tasks.release_task(db, task)
    db.commit()
    assert task.status is TaskStatus.OPEN
    assert task.assigned_user_id is None

    claimed = tasks.claim_task(db, organization_id=org_id, task_id=task.id, user_id=user_b)
    db.commit()
    assert claimed is not None
    assert claimed.assigned_user_id == user_b


def test_resolve_then_reopen_roundtrip(db, org_id):
    task = _make_task(db, org_id)
    tasks.resolve_task(db, task, resolution="done")
    db.commit()
    assert task.status is TaskStatus.RESOLVED
    assert task.completed_at is not None

    tasks.reopen_task(db, task)
    db.commit()
    assert task.status is TaskStatus.OPEN
    assert task.completed_at is None


def test_cancel_task(db, org_id):
    task = _make_task(db, org_id)
    tasks.cancel_task(db, task, reason="no longer needed")
    db.commit()
    assert task.status is TaskStatus.CANCELLED
