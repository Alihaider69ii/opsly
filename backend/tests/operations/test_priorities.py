from decimal import Decimal

from app.db.models import IssuePriority, IssueSeverity, RiskLevel
from app.services.operations.priorities import compute_priority


def test_default_is_normal_with_no_signals():
    result = compute_priority()
    assert result.priority is IssuePriority.NORMAL
    assert result.reasons


def test_sla_breach_alone_escalates_to_urgent():
    result = compute_priority(sla_breached=True)
    assert result.priority is IssuePriority.URGENT


def test_sla_breach_plus_high_value_escalates_to_urgent():
    result = compute_priority(sla_breached=True, order_value=Decimal("9999"))
    assert result.priority is IssuePriority.URGENT


def test_high_order_value_alone_is_high_not_urgent():
    result = compute_priority(order_value=Decimal("9999"))
    assert result.priority is IssuePriority.HIGH


def test_critical_risk_escalates():
    result = compute_priority(risk_level=RiskLevel.CRITICAL)
    assert result.priority is IssuePriority.URGENT


def test_medium_risk_escalates_less_than_critical():
    medium = compute_priority(risk_level=RiskLevel.MEDIUM)
    critical = compute_priority(risk_level=RiskLevel.CRITICAL)
    assert medium.score < critical.score
    assert medium.priority is IssuePriority.HIGH


def test_repeated_incidents_escalate():
    low = compute_priority(repeated_incidents=0)
    high = compute_priority(repeated_incidents=3)
    assert low.score < high.score
    assert high.priority is IssuePriority.URGENT


def test_severity_critical_adds_a_point_but_never_exceeds_urgent():
    result = compute_priority(
        sla_breached=True,
        order_value=Decimal("9999"),
        repeated_incidents=5,
        risk_level=RiskLevel.CRITICAL,
        severity=IssueSeverity.CRITICAL,
    )
    assert result.priority is IssuePriority.URGENT  # capped, never overflows the enum
