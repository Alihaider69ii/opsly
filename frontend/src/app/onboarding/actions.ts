"use server";

import { redirect } from "next/navigation";
import { ApiError, apiFetch } from "@/lib/api";
import { failure, text } from "@/lib/form";
import { clearSession, getToken } from "@/lib/session";
import type { FormState, Organization } from "@/lib/types";

// "example.com" -> "https://example.com"; the backend only accepts http(s) URLs.
function normalizeWebsite(value: string): string {
  if (!value) return "";
  return /^https?:\/\//i.test(value) ? value : `https://${value}`;
}

export async function createOrganization(_prev: FormState, formData: FormData): Promise<FormState> {
  // Server Actions are public endpoints: re-check the session here, don't trust the page.
  const token = await getToken();
  if (!token) redirect("/login");

  const values = {
    name: text(formData, "name"),
    business_type: text(formData, "business_type"),
    website: text(formData, "website"),
  };
  if (!values.name) {
    return {
      error: "Please check the highlighted fields.",
      fieldErrors: { name: "Business name is required" },
      values,
    };
  }

  try {
    // Onboarding creates the first business only: a double submit or a stale tab goes to the
    // dashboard instead of creating a duplicate.
    const existing = await apiFetch<Organization[]>("/api/organizations", { token });
    if (existing.length === 0) {
      await apiFetch<Organization>("/api/organizations", {
        method: "POST",
        token,
        body: {
          name: values.name,
          business_type: values.business_type || null,
          website: normalizeWebsite(values.website) || null,
        },
      });
    }
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await clearSession();
      redirect("/login");
    }
    return failure(error, values);
  }
  redirect("/dashboard");
}
