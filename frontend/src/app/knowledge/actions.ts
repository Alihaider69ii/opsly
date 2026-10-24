"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { ApiError, apiFetch } from "@/lib/api";
import { failure, text } from "@/lib/form";
import { isUuid } from "@/lib/format";
import { clearSession, getToken } from "@/lib/session";
import type { FormState, KnowledgeSource } from "@/lib/types";

// Server Actions are public endpoints: every one re-reads the session itself and lets the backend
// authorize the action (role checks and tenancy are enforced there, never in the UI).

async function sessionToken(): Promise<string> {
  const token = await getToken();
  if (!token) redirect("/login");
  return token;
}

async function guard<T>(run: (token: string) => Promise<T>, values?: Record<string, string>) {
  const token = await sessionToken();
  try {
    return { ok: true as const, data: await run(token) };
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await clearSession();
      redirect("/login");
    }
    return { ok: false as const, state: failure(error, values) };
  }
}

export async function addWebsite(_prev: FormState, formData: FormData): Promise<FormState> {
  const url = text(formData, "url");
  if (!url) {
    return {
      error: "Please check the highlighted fields.",
      fieldErrors: { url: "Enter your website address" },
      values: { url },
    };
  }
  const result = await guard(
    (token) =>
      apiFetch<KnowledgeSource>("/api/knowledge/sources", {
        method: "POST",
        token,
        body: { type: "WEBSITE", url },
      }),
    { url },
  );
  if (!result.ok) return result.state;
  revalidatePath("/knowledge");
  const source = result.data;
  if (source.status === "FAILED" && source.error_message) return { error: source.error_message };
  return { message: `Added ${source.name}. The crawl has been queued.` };
}

export async function recrawl(_prev: FormState, formData: FormData): Promise<FormState> {
  const id = text(formData, "source_id");
  if (!isUuid(id)) return { error: "Unknown knowledge source." };
  const result = await guard((token) =>
    apiFetch<KnowledgeSource>(`/api/knowledge/sources/${id}/crawl`, { method: "POST", token }),
  );
  if (!result.ok) return result.state;
  revalidatePath("/knowledge");
  revalidatePath(`/knowledge/${id}`);
  return result.data.error_message
    ? { error: result.data.error_message }
    : { message: "Crawl queued." };
}

export async function deleteSource(_prev: FormState, formData: FormData): Promise<FormState> {
  const id = text(formData, "source_id");
  if (!isUuid(id)) return { error: "Unknown knowledge source." };
  const result = await guard((token) =>
    apiFetch<void>(`/api/knowledge/sources/${id}`, { method: "DELETE", token }),
  );
  if (!result.ok) return result.state;
  revalidatePath("/knowledge");
  redirect("/knowledge");
}
