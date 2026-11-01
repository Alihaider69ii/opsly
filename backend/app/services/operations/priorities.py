"""Deterministic priority scoring (brief section 6).

A score is a sum of small, explainable points; the final band is always one of
URGENT/HIGH/NORMAL/LOW. The AI layer (``ai.py``) may *recommend* a priority by calling this same
function and presenting ``reasons`` -- it never invents a priority of its own, and a human's
explicit priority (``priority_source=HUMAN``) is never overwritten by a later call here (enforced
by callers in ``issues.py``, not by this function, which is a pure calculator).
"""

from dataclasses import dataclass, field
from decimal import Decimal

from app.db.models import IssuePriority, IssueSeverity, RiskLevel

_BANDS = [IssuePriority.LOW, IssuePriority.NORMAL, IssuePriority.HIGH, IssuePriority.URGENT]

DEFAULT_HIGH_VALUE_THRESHOLD = Decimal("5000")


@dataclass(frozen=True)
class PriorityResult:
    priority: IssuePriority
    score: int
    reasons: list[str] = field(default_factory=list)


def compute_priority(
    *,
    order_value: Decimal | None = None,
    sla_breached: bool = False,
    repeated_incidents: int = 0,
    risk_level: RiskLevel | None = None,
    severity: IssueSeverity | None = None,
    customer_waiting_minutes: int | None = None,
    high_value_threshold: Decimal = DEFAULT_HIGH_VALUE_THRESHOLD,
) -> PriorityResult:
    score = 1  # NORMAL baseline
    reasons: list[str] = []

    if sla_breached:
        score += 2
        reasons.append("SLA already breached")
    if risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
        score += 2
        reasons.append(f"risk level {risk_level.value}")
    elif risk_level is RiskLevel.MEDIUM:
        score += 1
        reasons.append("risk level MEDIUM")
    if order_value is not None and order_value >= high_value_threshold:
        score += 1
        reasons.append(f"order value >= {high_value_threshold}")
    if repeated_incidents >= 3:
        score += 2
        reasons.append(f"{repeated_incidents} repeated incidents")
    elif repeated_incidents >= 1:
        score += 1
        reasons.append(f"{repeated_incidents} repeated incident(s)")
    if severity is IssueSeverity.CRITICAL:
        score += 1
        reasons.append("severity CRITICAL")
    if customer_waiting_minutes is not None and customer_waiting_minutes >= 24 * 60:
        score += 1
        reasons.append(f"customer waiting {customer_waiting_minutes} minutes")

    band_index = max(0, min(score, len(_BANDS) - 1))
    if not reasons:
        reasons.append("no escalation signal; default priority")
    return PriorityResult(priority=_BANDS[band_index], score=score, reasons=reasons)
