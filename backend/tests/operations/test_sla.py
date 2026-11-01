from datetime import timedelta

from app.db.models import IssuePriority, SLACategory
from app.services.operations import sla
from tests.operations.conftest import NOW


def test_ensure_default_slas_is_idempotent(db, org_id):
    first = sla.ensure_default_slas(db, org_id)
    db.commit()
    second = sla.ensure_default_slas(db, org_id)
    db.commit()
    assert len(first) == len(sla.DEFAULT_SLA_DEFINITIONS)
    assert len(second) == len(first)


def test_select_sla_prefers_priority_specific_over_category_wide(db, org_id):
    sla.ensure_default_slas(db, org_id)
    db.commit()
    urgent = sla.select_sla(
        db, organization_id=org_id, category=SLACategory.FULFILLMENT, priority=IssuePriority.URGENT
    )
    standard = sla.select_sla(
        db, organization_id=org_id, category=SLACategory.FULFILLMENT, priority=IssuePriority.NORMAL
    )
    assert urgent is not None and standard is not None
    assert urgent.id != standard.id
    assert urgent.target_minutes < standard.target_minutes


def test_evaluate_reports_breach_after_target_minutes_elapse():
    class _Fake:
        target_minutes = 60

    not_yet = sla.evaluate(_Fake(), started_at=NOW, now=NOW + timedelta(minutes=30))
    assert not_yet.breached is False
    assert not_yet.remaining_minutes > 0

    breached = sla.evaluate(_Fake(), started_at=NOW, now=NOW + timedelta(minutes=90))
    assert breached.breached is True
    assert breached.remaining_minutes < 0


def test_evaluate_with_no_sla_never_breaches():
    result = sla.evaluate(None, started_at=NOW, now=NOW + timedelta(days=365))
    assert result.breached is False
    assert result.due_at is None
