"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { ApiError, apiFetch } from "@/lib/api";
import { failure, text } from "@/lib/form";
import { clearSession, getToken } from "@/lib/session";
import type {
  ConsentIn,
  FormState,
  WhatsAppAIConfidence,
  WhatsAppConsentList,
  WhatsAppConversation,
  WhatsAppStatus,
} from "@/lib/types";

// Server Actions are public endpoints: every one re-reads the session itself and lets the backend
// authorize the action (role checks and tenancy are enforced there, never in the UI). The browser
// never talks to Meta's API or ours with a credential: this file is the only bridge.

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
    return { ok: false as const, state: failure(error, values), error };
  }
}

export async function connectWhatsApp(_prev: FormState, formData: FormData): Promise<FormState> {
  const phone_number_id = text(formData, "phone_number_id");
  const waba_id = text(formData, "waba_id");
  const access_token = text(formData, "access_token");
  const default_country = text(formData, "default_country");
  const values = { phone_number_id, waba_id, access_token: "", default_country };
  if (!phone_number_id || !waba_id || !access_token) {
    return {
      error: "Please check the highlighted fields.",
      fieldErrors: {
        ...(!phone_number_id && { phone_number_id: "Required" }),
        ...(!waba_id && { waba_id: "Required" }),
        ...(!access_token && { access_token: "Required" }),
      },
      values,
    };
  }
  const result = await guard(
    (token) =>
      apiFetch("/api/integrations/whatsapp/connect", {
        method: "POST",
        token,
        body: {
          phone_number_id,
          waba_id,
          access_token,
          ...(default_country && { default_country: default_country.toUpperCase() }),
        },
      }),
    values,
  );
  if (!result.ok) return result.state;
  revalidatePath("/whatsapp");
  redirect("/whatsapp?connected=1");
}

export async function disconnectWhatsApp(): Promise<FormState> {
  const result = await guard((token) =>
    apiFetch<WhatsAppStatus>("/api/integrations/whatsapp/disconnect", { method: "POST", token }),
  );
  if (!result.ok) return result.state;
  revalidatePath("/whatsapp");
  redirect("/whatsapp?disconnected=1");
}

export async function syncWhatsAppTemplates(): Promise<FormState> {
  const result = await guard((token) =>
    apiFetch("/api/whatsapp/templates/sync", { method: "POST", token }),
  );
  if (!result.ok) return result.state;
  revalidatePath("/whatsapp");
  return { message: "Templates synced." };
}

export async function setWhatsAppAutoReply(enabled: boolean): Promise<FormState> {
  const result = await guard((token) =>
    apiFetch("/api/integrations/whatsapp/auto-reply", {
      method: "POST",
      token,
      body: { enabled },
    }),
  );
  if (!result.ok) return result.state;
  revalidatePath("/whatsapp");
  return { message: enabled ? "AI auto-reply enabled." : "AI auto-reply disabled." };
}

export async function setWhatsAppAISettings(settings: {
  grounding_required: boolean;
  min_confidence: WhatsAppAIConfidence;
  handoff_enabled: boolean;
  fallback_message: string | null;
  handoff_message: string | null;
  max_replies_per_window: number;
}): Promise<FormState> {
  const result = await guard((token) =>
    apiFetch("/api/integrations/whatsapp/ai-settings", {
      method: "POST",
      token,
      body: settings,
    }),
  );
  if (!result.ok) return result.state;
  revalidatePath("/whatsapp");
  return { message: "AI settings saved." };
}

export async function claimHandoff(
  conversationId: string,
): Promise<{ ok: true; data: WhatsAppConversation } | { ok: false; state: FormState }> {
  const result = await guard((token) =>
    apiFetch<WhatsAppConversation>(
      `/api/whatsapp/conversations/${conversationId}/handoff/claim`,
      { method: "POST", token },
    ),
  );
  if (!result.ok) return { ok: false, state: result.state };
  revalidatePath(`/whatsapp/conversations/${conversationId}`);
  return { ok: true, data: result.data };
}

export async function resolveHandoff(
  conversationId: string,
): Promise<{ ok: true; data: WhatsAppConversation } | { ok: false; state: FormState }> {
  const result = await guard((token) =>
    apiFetch<WhatsAppConversation>(
      `/api/whatsapp/conversations/${conversationId}/handoff/resolve`,
      { method: "POST", token },
    ),
  );
  if (!result.ok) return { ok: false, state: result.state };
  revalidatePath(`/whatsapp/conversations/${conversationId}`);
  return { ok: true, data: result.data };
}

export async function setConsent(
  contactId: string,
  body: ConsentIn,
): Promise<{ ok: true; data: WhatsAppConsentList } | { ok: false; state: FormState }> {
  const result = await guard((token) =>
    apiFetch<WhatsAppConsentList>(`/api/whatsapp/contacts/${contactId}/consent`, {
      method: "POST",
      token,
      body,
    }),
  );
  if (!result.ok) return { ok: false, state: result.state };
  revalidatePath(`/whatsapp/conversations`);
  return { ok: true, data: result.data };
}

export async function optOutContact(
  contactId: string,
  reason: string,
): Promise<{ ok: true; data: WhatsAppConsentList } | { ok: false; state: FormState }> {
  const result = await guard((token) =>
    apiFetch<WhatsAppConsentList>(`/api/whatsapp/contacts/${contactId}/opt-out`, {
      method: "POST",
      token,
      body: { reason },
    }),
  );
  if (!result.ok) return { ok: false, state: result.state };
  revalidatePath(`/whatsapp/conversations`);
  return { ok: true, data: result.data };
}
