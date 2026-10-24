"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { ApiError, apiFetch } from "@/lib/api";
import { failure } from "@/lib/form";
import { clearSession, getToken } from "@/lib/session";
import type { ChatWidget, FormState } from "@/lib/types";

// Server Actions are public endpoints: re-read the session here; the backend enforces roles.
export async function saveWidgetSettings(_prev: FormState, formData: FormData): Promise<FormState> {
  const token = await getToken();
  if (!token) redirect("/login");

  const welcome = String(formData.get("welcome_message") ?? "").trim();
  const origins = String(formData.get("allowed_origins") ?? "")
    .split(/[\n,]+/)
    .map((line) => line.trim())
    .filter(Boolean);
  const values = { welcome_message: welcome, allowed_origins: origins.join("\n") };

  try {
    await apiFetch<ChatWidget>("/api/chat/widget", {
      method: "PATCH",
      token,
      body: {
        enabled: formData.get("enabled") === "on",
        welcome_message: welcome,
        allowed_origins: origins,
      },
    });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await clearSession();
      redirect("/login");
    }
    return failure(error, values);
  }
  revalidatePath("/website-chat");
  return { message: "Settings saved." };
}
