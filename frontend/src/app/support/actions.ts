"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { ApiError, apiFetch } from "@/lib/api";
import { failure } from "@/lib/form";
import { clearSession, getToken } from "@/lib/session";
import type {
  FormState,
  SupportActionResult,
  SupportConversationSummary,
  SupportMessage,
  SupportPriority,
} from "@/lib/types";

// Server Actions are public endpoints: every one re-reads the session itself and lets the backend
// authorize the action (role checks and tenancy are enforced there, never in the UI).

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

function revalidate(conversationId: string) {
  revalidatePath("/support");
  revalidatePath(`/support/conversations/${conversationId}`);
}

export async function replyToConversation(
  conversationId: string,
  content: string,
): Promise<Result<SupportMessage>> {
  const result = await guard((token) =>
    apiFetch<SupportMessage>(`/api/support/conversations/${conversationId}/reply`, {
      method: "POST",
      token,
      body: { content },
    }),
  );
  if (result.ok) revalidate(conversationId);
  return result;
}

// Replying implicitly takes the conversation over (see services/support/conversations.py:
// send_human_reply), so the panel re-fetches the conversation afterward to pick up the new
// ai_state/status/assignment rather than trusting stale client state.
export async function getConversationSummary(
  conversationId: string,
): Promise<Result<SupportConversationSummary>> {
  return guard((token) =>
    apiFetch<SupportConversationSummary>(`/api/support/conversations/${conversationId}`, { token }),
  );
}

async function conversationAction(
  conversationId: string,
  path: string,
  body?: unknown,
): Promise<Result<SupportActionResult>> {
  const result = await guard((token) =>
    apiFetch<SupportActionResult>(`/api/support/conversations/${conversationId}/${path}`, {
      method: "POST",
      token,
      body: body ?? {},
    }),
  );
  if (result.ok) revalidate(conversationId);
  return result;
}

export async function takeOverConversation(id: string) {
  return conversationAction(id, "take-over");
}

export async function returnConversationToAi(id: string) {
  return conversationAction(id, "return-to-ai");
}

export async function requestHumanForConversation(id: string, reason: string) {
  return conversationAction(id, "request-human", { reason });
}

export async function resolveConversation(id: string) {
  return conversationAction(id, "resolve");
}

export async function reopenConversation(id: string) {
  return conversationAction(id, "reopen");
}

export async function claimConversation(id: string) {
  return conversationAction(id, "claim");
}

export async function releaseConversation(id: string) {
  return conversationAction(id, "release");
}

export async function assignConversation(id: string, agentUserId: string) {
  return conversationAction(id, "assign", { agent_user_id: agentUserId });
}

export async function setConversationPriority(
  id: string,
  priority: SupportPriority,
  reason?: string,
) {
  return conversationAction(id, "priority", { priority, reason: reason || null });
}
