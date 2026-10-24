"use server";

import { redirect } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { failure, text } from "@/lib/form";
import { forwardedFor } from "@/lib/request";
import { clearSession, setSession } from "@/lib/session";
import type { FormState, Organization, TokenResponse } from "@/lib/types";

// Logs in against the backend, stores the token in the HttpOnly cookie, and returns where the
// user should land: the dashboard if they already have a business, otherwise onboarding.
async function startSession(email: string, password: string): Promise<string> {
  const session = await apiFetch<TokenResponse>("/api/auth/login", {
    method: "POST",
    body: { email, password },
    forwardedFor: await forwardedFor(),
  });
  await setSession(session.access_token, session.expires_in);
  const orgs = await apiFetch<Organization[]>("/api/organizations", {
    token: session.access_token,
  });
  return orgs.length > 0 ? "/dashboard" : "/onboarding";
}

export async function login(_prev: FormState, formData: FormData): Promise<FormState> {
  const email = text(formData, "email");
  const password = String(formData.get("password") ?? ""); // passwords are never trimmed

  let destination: string;
  try {
    destination = await startSession(email, password);
  } catch (error) {
    return failure(error, { email });
  }
  redirect(destination);
}

export async function signup(_prev: FormState, formData: FormData): Promise<FormState> {
  const email = text(formData, "email");
  const fullName = text(formData, "full_name");
  const password = String(formData.get("password") ?? "");

  try {
    await apiFetch("/api/auth/register", {
      method: "POST",
      body: { email, password, full_name: fullName || null },
      forwardedFor: await forwardedFor(),
    });
  } catch (error) {
    return failure(error, { email, full_name: fullName });
  }

  // Account exists now. If the follow-up login fails for any reason, the login page is the
  // right fallback rather than showing an error for something that succeeded.
  let destination = "/login";
  try {
    destination = await startSession(email, password);
  } catch {
    // fall through to /login
  }
  redirect(destination);
}

export async function logout(): Promise<void> {
  await clearSession();
  redirect("/login");
}
