"""Mutants for Phase 5 event routing, loop protection and durable action semantics."""

M: list = []


def add(mid, file, old, new, args):
    M.append((mid, file, old, new, args))


AUTO = "app/services/automation.py"
UNIT = ["tests/test_automation.py"]

add(
    "auto-01-unknown-condition-fails-open",
    AUTO,
    "        else:\n            return False\n        if not result:",
    "        else:\n            result = True\n        if not result:",
    UNIT,
)
add(
    "auto-02-inactive-workflow-dispatches",
    AUTO,
    "                AutomationWorkflow.status == WorkflowStatus.ACTIVE,",
    "                True,",
    UNIT,
)
add(
    "auto-03-processed-event-dispatches-again",
    AUTO,
    "    if event is None or event.status != EventStatus.PENDING:\n        return 0",
    "    if event is None:\n        return 0",
    UNIT,
)
add(
    "auto-04-depth-limit-off-by-one",
    AUTO,
    "event_type not in EVENT_TYPES or depth > MAX_DEPTH",
    "event_type not in EVENT_TYPES or depth >= MAX_DEPTH",
    UNIT,
)
add(
    "auto-05-task-side-effect-removed",
    AUTO,
    "            db.add(task)\n            step.status = StepStatus.COMPLETED",
    "            pass\n            step.status = StepStatus.COMPLETED",
    UNIT,
)
add(
    "auto-06-customer-tag-is-not-set-like",
    AUTO,
    '                customer.tags = list(dict.fromkeys([*(customer.tags or []), action["tag"]]))[:100]',
    '                customer.tags = [*(customer.tags or []), action["tag"]][:100]',
    UNIT,
)
add(
    "auto-07-due-wait-not-recovered",
    AUTO,
    "                    (AutomationExecution.status == ExecutionStatus.WAITING)\n                    & (AutomationExecution.next_run_at <= now)",
    "                    False",
    UNIT,
)
