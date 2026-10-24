"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { ApiError, apiFetch } from "@/lib/api";
import { clearSession, getToken } from "@/lib/session";

async function token() {
  const value = await getToken();
  if (!value) redirect("/login");
  return value;
}

export async function createAutomation(_previous: { error?: string; message?: string } | null, formData: FormData) {
  const auth = await token();
  const name = String(formData.get("name") || "").trim();
  const event = String(formData.get("event") || "");
  const action = String(formData.get("action") || "");
  const text = String(formData.get("action_text") || "").trim();
  const threshold = String(formData.get("threshold") || "").trim();
  const conditionEnabled = formData.get("condition") === "on";
  const actions: Record<string, unknown>[] = [];
  if (action === "wait") actions.push({ type: "wait", seconds: Number(formData.get("seconds")) });
  else if (["log", "create_task", "ai_generate"].includes(action)) {
    const key = action === "log" ? "message" : action === "create_task" ? "title" : "prompt";
    actions.push({ type: action, [key]: text });
  }
  if (!name || !event || actions.length === 0) return { error: "Enter a name and complete the workflow fields." };
  const conditions = conditionEnabled
    ? [{ field: event === "cart_abandoned" ? "cart.total" : "order.total", op: "gt", value: Number(threshold) }]
    : [];
  try {
    await apiFetch("/api/automations", {
      method: "POST",
      token: auth,
      body: { name, definition: { trigger: { event }, conditions, actions } },
    });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await clearSession();
      redirect("/login");
    }
    return { error: error instanceof Error ? error.message : "Could not create workflow." };
  }
  revalidatePath("/automations");
  return { message: "Workflow saved as a draft." };
}

async function changeStatus(formData: FormData, status: "activate" | "pause" | "archive") {
  const auth = await token();
  const id = String(formData.get("id") || "");
  if (!/^[0-9a-f-]{36}$/i.test(id)) return;
  try {
    await apiFetch(`/api/automations/${id}/${status}`, { method: "POST", token: auth });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await clearSession();
      redirect("/login");
    }
  }
  revalidatePath("/automations");
}

export async function activateAutomation(formData: FormData) {
  return changeStatus(formData, "activate");
}

export async function pauseAutomation(formData: FormData) {
  return changeStatus(formData, "pause");
}

export async function archiveAutomation(formData: FormData) {
  return changeStatus(formData, "archive");
}
