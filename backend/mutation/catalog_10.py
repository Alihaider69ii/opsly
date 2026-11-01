"""Mutants for Phase 10: D2C Operations AI -- tenant scoping, authorization, priority, SLA,
approval, the destructive action gate, issue state transitions, task claim and AI grounding/
tenant isolation.
"""

M: list = []


def add(mid, file, old, new, args):
    M.append((mid, file, old, new, args))


ISSUES = "app/services/operations/issues.py"
TASKS = "app/services/operations/tasks.py"
ACTIONS = "app/services/operations/actions.py"
REPOSITORY = "app/services/operations/repository.py"
ROUTER = "app/api/operations/router.py"
AI = "app/services/operations/ai.py"

API_TESTS = ["tests/operations/test_api.py"]
ISSUES_TESTS = ["tests/operations/test_issues.py"]
TASKS_TESTS = ["tests/operations/test_tasks.py"]
ACTIONS_TESTS = ["tests/operations/test_actions.py"]
AI_TESTS = ["tests/operations/test_ai.py"]
POLICY_TESTS = ["tests/test_tenant_api_matrix.py", "tests/operations/test_api.py"]

# ---- tenant scoping -----------------------------------------------------------------------------
add(
    "op-01-get-issue-drops-tenant-filter",
    REPOSITORY,
    "def get_issue(\n"
    "    db: Session, *, organization_id: uuid.UUID, issue_id: uuid.UUID\n"
    ") -> OperationalIssue | None:\n"
    "    return db.scalar(\n"
    "        select(OperationalIssue).where(\n"
    "            OperationalIssue.id == issue_id, OperationalIssue.organization_id == organization_id\n"
    "        )\n"
    "    )",
    "def get_issue(\n"
    "    db: Session, *, organization_id: uuid.UUID, issue_id: uuid.UUID\n"
    ") -> OperationalIssue | None:\n"
    "    return db.scalar(\n"
    "        select(OperationalIssue).where(\n"
    "            OperationalIssue.id == issue_id\n"
    "        )\n"
    "    )",
    API_TESTS,
)

# ---- authorization --------------------------------------------------------------------------------
add(
    "op-02-approve-action-skips-manager-gate",
    ROUTER,
    "def approve_action_endpoint(action_id: uuid.UUID, ctx: ManagerOrg, db: DbSession) -> dict[str, Any]:",
    "def approve_action_endpoint(action_id: uuid.UUID, ctx: CurrentOrg, db: DbSession) -> dict[str, Any]:",
    POLICY_TESTS,
)

# ---- priority: a human's priority must never be silently overridden -------------------------------
add(
    "op-03-ai-can-override-human-priority",
    ISSUES,
    "    if (\n"
    "        issue.priority_source == OperationsPrioritySource.HUMAN\n"
    "        and source != OperationsPrioritySource.HUMAN\n"
    "    ):",
    "    if False:",
    ISSUES_TESTS,
)

# ---- SLA: a breach is never recorded ----------------------------------------------------------------
add(
    "op-04-sla-breach-never-flagged",
    ISSUES,
    "    if (\n        result.breached\n        and issue.breached_at is None\n        and issue.status\n",
    "    if (\n        False\n        and issue.breached_at is None\n        and issue.status\n",
    ISSUES_TESTS,
)

# ---- approval / idempotency: an action can execute (and re-execute) without being APPROVED --------
add(
    "op-05-execute-ignores-approval-status",
    ACTIONS,
    "    if action.status is not ActionStatus.APPROVED:\n"
    '        raise ActionError(f"action is {action.status.value}, not APPROVED")',
    "    if False:\n"
    '        raise ActionError(f"action is {action.status.value}, not APPROVED")',
    ACTIONS_TESTS,
)

# ---- destructive action gate: policy classification ------------------------------------------------
add(
    "op-06-refund-downgraded-to-safe",
    ACTIONS,
    '    "refund": ActionCategory.REQUIRES_APPROVAL,',
    '    "refund": ActionCategory.SAFE_AUTOMATION,',
    ACTIONS_TESTS,
)

# ---- destructive action gate: FORBIDDEN bypass -----------------------------------------------------
add(
    "op-07-forbidden-action-not-failed",
    ACTIONS,
    "    if category is ActionCategory.FORBIDDEN:\n"
    "        action.status = ActionStatus.FAILED\n"
    '        action.error_code = "forbidden_action_type"',
    "    if False:\n"
    "        action.status = ActionStatus.FAILED\n"
    '        action.error_code = "forbidden_action_type"',
    ACTIONS_TESTS,
)

# ---- issue state transitions: a resolved issue is never reopened by a recurrence -------------------
add(
    "op-08-resolved-issue-never-reopens",
    ISSUES,
    "        reopened = existing.status in (IssueStatus.RESOLVED, IssueStatus.DISMISSED)",
    "        reopened = False",
    ISSUES_TESTS,
)

# ---- task claim: the claimed task's final state is wrong (status never flips to ASSIGNED) ---------
add(
    "op-09-claim-leaves-status-open",
    TASKS,
    "        .values(assigned_user_id=user_id, status=TaskStatus.ASSIGNED, updated_at=now)",
    "        .values(assigned_user_id=user_id, updated_at=now)",
    TASKS_TESTS,
)

# ---- task claim: the rowcount guard is ignored, so a losing claim still reports success ------------
add(
    "op-10-claim-ignores-rowcount-guard",
    TASKS,
    "    if result.rowcount != 1:  # type: ignore[attr-defined]\n        return None",
    "    if False:\n        return None",
    TASKS_TESTS,
)

# ---- AI grounding: an unrelated question is answered anyway (never refuses) -----------------------
add(
    "op-11-ai-never-refuses-ungrounded-question",
    AI,
    "    if not context:\n        return AnswerResult(summary=INSUFFICIENT_DATA, grounded=False)",
    "    if False:\n        return AnswerResult(summary=INSUFFICIENT_DATA, grounded=False)",
    AI_TESTS,
)

# ---- AI tenant isolation: order lookup drops the organization filter -------------------------------
add(
    "op-12-ai-order-lookup-drops-tenant-filter",
    AI,
    "        order = (\n"
    "            db.query(Order)\n"
    "            .filter(Order.organization_id == organization_id, Order.order_number == value)\n"
    "            .first()\n"
    "        )",
    "        order = (\n"
    "            db.query(Order)\n"
    "            .filter(Order.order_number == value)\n"
    "            .first()\n"
    "        )",
    AI_TESTS,
)
