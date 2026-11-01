"use client";

import { useState, useTransition } from "react";
import { PriorityBadge } from "@/components/priority-badge";
import { StatusBadge } from "@/components/status-badge";
import {
  approveAction,
  assignIssue,
  createTask,
  dismissIssue,
  executeAction,
  rejectAction,
  reopenIssue,
  requestAction,
  resolveIssue,
  setIssuePriority,
} from "../../actions";
import type { OperationalAction, OperationalIssue, OperationsPriority } from "@/lib/types";
import { formatDateTime } from "@/lib/format";

const PRIORITIES: OperationsPriority[] = ["URGENT", "HIGH", "NORMAL", "LOW"];
const ACTION_TYPES = [
  "create_task",
  "change_priority",
  "refund",
  "cancel_order",
  "inventory_adjustment",
] as const;

export function IssuePanel({
  issueId,
  initial,
  currentUserId,
}: {
  issueId: string;
  initial: OperationalIssue;
  currentUserId: string;
}) {
  const [issue, setIssue] = useState(initial);
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [requestedAction, setRequestedAction] = useState<OperationalAction | null>(null);
  const [actionType, setActionType] = useState<(typeof ACTION_TYPES)[number]>("create_task");

  function run(fn: () => Promise<{ ok: true; data: OperationalIssue } | { ok: false; state: { error?: string } | null }>) {
    setError(null);
    startTransition(async () => {
      const result = await fn();
      if (!result.ok) {
        setError(result.state?.error ?? "Something went wrong.");
        return;
      }
      setIssue(result.data);
    });
  }

  function runAction(fn: () => Promise<{ ok: true; data: OperationalAction } | { ok: false; state: { error?: string } | null }>) {
    setError(null);
    startTransition(async () => {
      const result = await fn();
      if (!result.ok) {
        setError(result.state?.error ?? "Something went wrong.");
        return;
      }
      setRequestedAction(result.data);
    });
  }

  const resolved = issue.status === "RESOLVED" || issue.status === "DISMISSED";

  return (
    <div className="space-y-6">
      <div
        data-testid="issue-status"
        className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800"
      >
        <div className="flex items-center gap-2">
          <StatusBadge status={issue.category} />
          <StatusBadge status={issue.severity} />
          <StatusBadge status={issue.status} />
          <PriorityBadge priority={issue.priority} />
        </div>
        <div className="flex flex-wrap gap-2">
          {!issue.assigned_user_id ? (
            <ActionButton label="Assign to me" onClick={() => run(() => assignIssue(issueId, currentUserId))} disabled={pending} />
          ) : null}
          {resolved ? (
            <ActionButton label="Reopen" onClick={() => run(() => reopenIssue(issueId))} disabled={pending} />
          ) : (
            <>
              <ActionButton label="Resolve" onClick={() => run(() => resolveIssue(issueId))} disabled={pending} />
              <ActionButton label="Dismiss" onClick={() => run(() => dismissIssue(issueId))} disabled={pending} />
            </>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 text-sm">
        <span className="text-zinc-500">Priority:</span>
        {PRIORITIES.map((p) => (
          <button
            key={p}
            type="button"
            disabled={pending}
            onClick={() => run(() => setIssuePriority(issueId, p))}
            className={`rounded-md px-2 py-1 text-xs ${
              issue.priority === p ? "ring-1 ring-zinc-900 dark:ring-zinc-100" : "hover:bg-zinc-100 dark:hover:bg-zinc-900"
            }`}
          >
            <PriorityBadge priority={p} />
          </button>
        ))}
      </div>

      {error ? (
        <p role="alert" className="text-sm text-red-600">
          {error}
        </p>
      ) : null}

      <section aria-label="Evidence" data-testid="issue-evidence" className="space-y-2">
        <h2 className="text-sm font-semibold tracking-tight">Evidence</h2>
        <pre className="overflow-x-auto rounded-lg border border-zinc-200 bg-zinc-50 p-3 text-xs dark:border-zinc-800 dark:bg-zinc-950">
          {JSON.stringify(issue.evidence, null, 2)}
        </pre>
      </section>

      <section aria-label="AI analysis" className="space-y-1 rounded-lg border border-zinc-200 p-4 text-sm dark:border-zinc-800">
        <h2 className="text-sm font-semibold tracking-tight">AI analysis &amp; recommendation</h2>
        <p className="text-zinc-600 dark:text-zinc-400">
          {issue.description ?? "No additional description."}
        </p>
        <p>
          <span className="text-zinc-500">Recommended action: </span>
          {issue.recommended_action ?? "none"}
        </p>
        <p className="text-xs text-zinc-500">
          Detected by {issue.detected_by}
          {issue.detector_code ? ` (${issue.detector_code})` : ""} · SLA due{" "}
          {issue.due_at ? formatDateTime(issue.due_at) : "not set"}
          {issue.breached_at ? " · BREACHED" : ""}
        </p>
      </section>

      <section aria-label="Safe action framework" data-testid="action-framework" className="space-y-3 rounded-lg border border-zinc-200 p-4 text-sm dark:border-zinc-800">
        <h2 className="text-sm font-semibold tracking-tight">Request an action</h2>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={actionType}
            onChange={(event) => setActionType(event.target.value as (typeof ACTION_TYPES)[number])}
            className="rounded-md border border-zinc-300 bg-white px-2 py-1 text-sm dark:border-zinc-700 dark:bg-zinc-950"
          >
            {ACTION_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
          <ActionButton
            label="Request"
            disabled={pending}
            onClick={() =>
              runAction(() =>
                requestAction({
                  action_type: actionType,
                  resource_type: "operational_issue",
                  resource_id: issueId,
                  idempotency_key: `${issueId}-${actionType}-${Date.now()}`,
                  payload:
                    actionType === "create_task"
                      ? { title: `Follow up: ${issue.title}`, category: issue.category }
                      : actionType === "change_priority"
                        ? { priority: "URGENT" }
                        : actionType === "refund"
                          ? { amount: "0.00" }
                          : actionType === "inventory_adjustment"
                            ? { delta: -1 }
                            : {},
                }),
              )
            }
          />
        </div>
        {requestedAction ? (
          <div data-testid="requested-action" className="space-y-2 rounded-md border border-zinc-200 p-3 dark:border-zinc-800">
            <div className="flex items-center gap-2">
              <StatusBadge status={requestedAction.category} />
              <StatusBadge status={requestedAction.status} />
              <span className="text-xs text-zinc-500">{requestedAction.action_type}</span>
            </div>
            {requestedAction.category === "REQUIRES_APPROVAL" && requestedAction.status === "PENDING" ? (
              <p className="text-xs text-amber-700 dark:text-amber-400">
                This action requires approval before it can execute.
              </p>
            ) : null}
            <div className="flex flex-wrap gap-2">
              {requestedAction.status === "PENDING" ? (
                <>
                  <ActionButton label="Approve" disabled={pending} onClick={() => runAction(() => approveAction(requestedAction.id))} />
                  <ActionButton label="Reject" disabled={pending} onClick={() => runAction(() => rejectAction(requestedAction.id))} />
                </>
              ) : null}
              {requestedAction.status === "APPROVED" ? (
                <ActionButton label="Execute" disabled={pending} onClick={() => runAction(() => executeAction(requestedAction.id))} />
              ) : null}
            </div>
            {requestedAction.result ? (
              <pre className="overflow-x-auto text-xs text-zinc-500">{JSON.stringify(requestedAction.result, null, 2)}</pre>
            ) : null}
          </div>
        ) : null}
      </section>

      <section aria-label="Tasks" data-testid="issue-tasks" className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold tracking-tight">Tasks</h2>
          <ActionButton
            label="Create task"
            disabled={pending}
            onClick={() =>
              startTransition(async () => {
                setError(null);
                const result = await createTask({
                  title: `Follow up: ${issue.title}`,
                  category: issue.category,
                  issue_id: issueId,
                });
                if (!result.ok) {
                  setError(result.state?.error ?? "Something went wrong.");
                  return;
                }
                setIssue((prev) => ({ ...prev, tasks: [...(prev.tasks ?? []), result.data] }));
              })
            }
          />
        </div>
        <ul className="space-y-2">
          {(issue.tasks ?? []).map((task) => (
            <li key={task.id} className="flex items-center justify-between gap-2 rounded-lg border border-zinc-200 p-3 text-sm dark:border-zinc-800">
              <span>{task.title}</span>
              <StatusBadge status={task.status} />
            </li>
          ))}
          {(issue.tasks ?? []).length === 0 ? <li className="text-sm text-zinc-500">No tasks yet.</li> : null}
        </ul>
      </section>

      <section aria-label="Audit history" data-testid="issue-audit" className="space-y-2">
        <h2 className="text-sm font-semibold tracking-tight">Audit history</h2>
        <ul className="space-y-1 text-sm">
          {(issue.audit ?? []).map((event) => (
            <li key={event.id} className="flex justify-between gap-2 text-zinc-600 dark:text-zinc-400">
              <span>{event.event_type.replace(/_/g, " ")}</span>
              <span className="text-xs text-zinc-500">{formatDateTime(event.created_at)}</span>
            </li>
          ))}
          {(issue.audit ?? []).length === 0 ? <li className="text-zinc-500">No audit events yet.</li> : null}
        </ul>
      </section>
    </div>
  );
}

function ActionButton({ label, onClick, disabled }: { label: string; onClick: () => void; disabled: boolean }) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      data-action={label.toLowerCase().replace(/\s+/g, "-")}
      className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
    >
      {label}
    </button>
  );
}
