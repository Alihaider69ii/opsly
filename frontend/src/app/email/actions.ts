"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { ApiError, apiFetch } from "@/lib/api";
import { failure, text } from "@/lib/form";
import { clearSession, getToken } from "@/lib/session";
import type {
  EmailCampaign,
  EmailClassification,
  EmailStatus,
  EmailTemplate,
  EmailTemplateStatus,
  FormState,
  WhatsAppAIConfidence,
} from "@/lib/types";

async function sessionToken(): Promise<string> {
  const token = await getToken();
  if (!token) redirect("/login");
  return token;
}

type Result<T> = { ok: true; data: T } | { ok: false; state: FormState };

async function guard<T>(run: (token: string) => Promise<T>, values?: Record<string, string>): Promise<Result<T>> {
  const token = await sessionToken();
  try {
    return { ok: true, data: await run(token) };
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await clearSession();
      redirect("/login");
    }
    return { ok: false, state: failure(error, values) };
  }
}

export async function connectEmail(_prev: FormState, formData: FormData): Promise<FormState> {
  const from_address = text(formData, "from_address");
  const from_name = text(formData, "from_name");
  const reply_to_address = text(formData, "reply_to_address");
  const values = { from_address, from_name, reply_to_address };
  if (!from_address) {
    return { error: "Please check the highlighted fields.", fieldErrors: { from_address: "Required" }, values };
  }
  const result = await guard(
    (token) =>
      apiFetch("/api/integrations/email/connect", {
        method: "POST",
        token,
        body: {
          from_address,
          from_name: from_name || null,
          reply_to_address: reply_to_address || null,
        },
      }),
    values,
  );
  if (!result.ok) return result.state;
  revalidatePath("/email");
  redirect("/email?connected=1");
}

export async function disconnectEmail(): Promise<FormState> {
  const result = await guard((token) =>
    apiFetch<EmailStatus>("/api/integrations/email/disconnect", { method: "POST", token }),
  );
  if (!result.ok) return result.state;
  revalidatePath("/email");
  redirect("/email?disconnected=1");
}

export async function setEmailAutoReply(enabled: boolean): Promise<FormState> {
  const result = await guard((token) =>
    apiFetch("/api/integrations/email/auto-reply", { method: "POST", token, body: { enabled } }),
  );
  if (!result.ok) return result.state;
  revalidatePath("/email");
  return { message: enabled ? "AI auto-reply enabled." : "AI auto-reply disabled." };
}

export async function setEmailAISettings(settings: {
  grounding_required: boolean;
  min_confidence: WhatsAppAIConfidence;
  handoff_enabled: boolean;
  fallback_message: string | null;
  handoff_message: string | null;
  max_replies_per_window: number;
}): Promise<FormState> {
  const result = await guard((token) =>
    apiFetch("/api/integrations/email/ai-settings", { method: "POST", token, body: settings }),
  );
  if (!result.ok) return result.state;
  revalidatePath("/email");
  return { message: "AI settings saved." };
}

export type TemplateInput = {
  name: string;
  type: EmailClassification;
  subject: string;
  preheader: string | null;
  body_html: string;
  body_text: string | null;
  variables: string[];
};

export async function createTemplate(input: TemplateInput): Promise<Result<EmailTemplate>> {
  const result = await guard((token) =>
    apiFetch<EmailTemplate>("/api/email/templates", { method: "POST", token, body: input }),
  );
  if (result.ok) revalidatePath("/email/templates");
  return result;
}

export async function updateTemplate(id: string, input: TemplateInput): Promise<Result<EmailTemplate>> {
  const result = await guard((token) =>
    apiFetch<EmailTemplate>(`/api/email/templates/${id}`, { method: "PATCH", token, body: input }),
  );
  if (result.ok) {
    revalidatePath("/email/templates");
    revalidatePath(`/email/templates/${id}`);
  }
  return result;
}

export async function setTemplateStatus(id: string, status: EmailTemplateStatus): Promise<Result<EmailTemplate>> {
  const result = await guard((token) =>
    apiFetch<EmailTemplate>(`/api/email/templates/${id}/status`, { method: "POST", token, body: { status } }),
  );
  if (result.ok) {
    revalidatePath("/email/templates");
    revalidatePath(`/email/templates/${id}`);
  }
  return result;
}

export async function previewTemplate(
  id: string,
  context: Record<string, unknown>,
): Promise<Result<{ subject: string; html: string; text: string }>> {
  return guard((token) =>
    apiFetch(`/api/email/templates/${id}/preview`, { method: "POST", token, body: context }),
  );
}

export async function createCampaign(input: {
  name: string;
  template_id: string;
  audience_definition: Record<string, unknown>;
}): Promise<Result<EmailCampaign>> {
  const result = await guard((token) =>
    apiFetch<EmailCampaign>("/api/email/campaigns", { method: "POST", token, body: input }),
  );
  if (result.ok) revalidatePath("/email/campaigns");
  return result;
}

async function campaignAction(id: string, path: string): Promise<Result<EmailCampaign>> {
  const result = await guard((token) =>
    apiFetch<EmailCampaign>(`/api/email/campaigns/${id}/${path}`, { method: "POST", token }),
  );
  if (result.ok) {
    revalidatePath("/email/campaigns");
    revalidatePath(`/email/campaigns/${id}`);
  }
  return result;
}

export async function startCampaign(id: string) {
  return campaignAction(id, "start");
}

export async function pauseCampaign(id: string) {
  return campaignAction(id, "pause");
}

export async function resumeCampaign(id: string) {
  return campaignAction(id, "resume");
}

export async function cancelCampaign(id: string) {
  return campaignAction(id, "cancel");
}
