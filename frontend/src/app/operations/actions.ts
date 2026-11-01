"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { ApiError, apiFetch } from "@/lib/api";
import { failure } from "@/lib/form";
import { clearSession, getToken } from "@/lib/session";
import type {
  AIQueryResult,
  FormState,
  IssueCategory,
  OperationalAction,
  OperationalIssue,
  OperationalTask,
  OperationsPriority,
} from "@/lib/types";

// Server Actions are public endpoints: every one re-reads the session itself and lets the backend
// authorize and tenant-scope the action (never the UI) -- mirrors app/support/actions.ts.

async function sessionToken(): Promise<string> {
  const token = await getToken();
  if (!token) redirect("/login");
  return token;
}

type Result<T> = { ok: true; data: T } | { ok: false; state: FormState };

async function guard<T>(run: (token: string) => Promise<T>): Promise<Result<T>> {
  const token = await sessionToken();
  try {
    return { ok: true, data: await run(token) };
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await clearSession();
      redirect("/login");
    }
    return { ok: false, state: failure(error) };
  }
}

function revalidate(extra?: string) {
  revalidatePath("/operations");
  revalidatePath("/operations/tasks");
  if (extra) revalidatePath(extra);
}

export async function runSweepNow() {
  const result = await guard((token) =>
    apiFetch<{ created: number; updated: number; breached: number }>("/api/operations/sweep", {
      method: "POST",
      token,
    }),
  );
  if (result.ok) revalidate();
  return result;
}

export async function createIssue(input: {
  category: IssueCategory;
  title: string;
  description?: string;
  recommended_action?: string;
}) {
  const result = await guard((token) =>
    apiFetch<OperationalIssue>("/api/operations/issues", { method: "POST", token, body: input }),
  );
  if (result.ok) revalidate();
  return result;
}

async function issueAction(id: string, path: string, body?: unknown, method: "POST" | "PATCH" = "POST") {
  const result = await guard((token) =>
    apiFetch<OperationalIssue>(`/api/operations/issues/${id}${path}`, { method, token, body: body ?? {} }),
  );
  if (result.ok) revalidate(`/operations/issues/${id}`);
  return result;
}

export async function assignIssue(id: string, assignedUserId: string) {
  return issueAction(id, "/assign", { assigned_user_id: assignedUserId });
}

export async function resolveIssue(id: string) {
  return issueAction(id, "/resolve");
}

export async function reopenIssue(id: string) {
  return issueAction(id, "/reopen");
}

export async function dismissIssue(id: string) {
  return issueAction(id, "/dismiss");
}

export async function setIssuePriority(id: string, priority: OperationsPriority) {
  return issueAction(id, "", { priority }, "PATCH");
}

export async function createTask(input: {
  title: string;
  category: IssueCategory;
  issue_id?: string;
  priority?: OperationsPriority;
}) {
  const result = await guard((token) =>
    apiFetch<OperationalTask>("/api/operations/tasks", { method: "POST", token, body: input }),
  );
  if (result.ok) revalidate(input.issue_id ? `/operations/issues/${input.issue_id}` : undefined);
  return result;
}

async function taskAction(id: string, path: string, body?: unknown) {
  const result = await guard((token) =>
    apiFetch<OperationalTask>(`/api/operations/tasks/${id}${path}`, { method: "POST", token, body: body ?? {} }),
  );
  if (result.ok) revalidate();
  return result;
}

export async function claimTask(id: string) {
  return taskAction(id, "/claim");
}

export async function releaseTask(id: string) {
  return taskAction(id, "/release");
}

export async function escalateTask(id: string) {
  return taskAction(id, "/escalate");
}

export async function resolveTask(id: string, resolution?: string) {
  return taskAction(id, "/resolve", { resolution: resolution || null });
}

export async function cancelTask(id: string, reason?: string) {
  return taskAction(id, "/cancel", { reason: reason || null });
}

export async function requestAction(input: {
  action_type: string;
  resource_type: string;
  resource_id?: string | null;
  idempotency_key: string;
  payload?: Record<string, unknown>;
}) {
  const result = await guard((token) =>
    apiFetch<OperationalAction>("/api/operations/actions", { method: "POST", token, body: input }),
  );
  if (result.ok) revalidate();
  return result;
}

async function actionOnAction(id: string, path: string) {
  const result = await guard((token) =>
    apiFetch<OperationalAction>(`/api/operations/actions/${id}${path}`, { method: "POST", token }),
  );
  if (result.ok) revalidate();
  return result;
}

export async function approveAction(id: string) {
  return actionOnAction(id, "/approve");
}

export async function rejectAction(id: string) {
  return actionOnAction(id, "/reject");
}

export async function executeAction(id: string) {
  return actionOnAction(id, "/execute");
}

export async function askOperationsAI(question: string) {
  return guard((token) =>
    apiFetch<AIQueryResult>("/api/operations/ai/query", { method: "POST", token, body: { question } }),
  );
}
