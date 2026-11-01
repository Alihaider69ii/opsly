"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { ApiError, apiFetch } from "@/lib/api";
import { failure, text } from "@/lib/form";
import { forwardedFor } from "@/lib/request";
import {
  clearAdminViewOrg,
  clearSession,
  getToken,
  setAdminViewOrg,
  setSession,
} from "@/lib/session";
import type { BrandCreated, FormState, TokenResponse } from "@/lib/types";

async function guard(): Promise<string> {
  const token = await getToken();
  if (!token) redirect("/admin/login");
  return token;
}

export async function adminLogin(_prev: FormState, formData: FormData): Promise<FormState> {
  const email = text(formData, "email");
  const password = String(formData.get("password") ?? "");

  let session: TokenResponse;
  try {
    session = await apiFetch<TokenResponse>("/api/auth/login", {
      method: "POST",
      body: { email, password },
      forwardedFor: await forwardedFor(),
    });
  } catch (error) {
    return failure(error, { email });
  }
  if (!session.user.is_platform_admin) {
    return { error: "This login is for platform administrators only.", values: { email } };
  }
  await setSession(session.access_token, session.expires_in);
  redirect("/admin");
}

export async function adminLogout(): Promise<void> {
  await clearAdminViewOrg();
  await clearSession();
  redirect("/admin/login");
}

export async function createBrand(_prev: FormState, formData: FormData): Promise<FormState> {
  const token = await guard();
  const name = text(formData, "name");
  const slug = text(formData, "slug");
  const environment = text(formData, "environment") || "PRODUCTION";
  const owner_name = text(formData, "owner_name");
  const owner_email = text(formData, "owner_email");
  const temporary_password = String(formData.get("temporary_password") ?? "");

  try {
    await apiFetch<BrandCreated>("/api/platform/brands", {
      method: "POST",
      token,
      body: {
        name,
        slug: slug || null,
        environment,
        owner_name,
        owner_email,
        temporary_password,
      },
    });
  } catch (error) {
    return failure(error, { name, slug, owner_name, owner_email });
  }
  revalidatePath("/admin/brands");
  return { message: `${name} created. Owner: ${owner_email}.` };
}

export async function setBrandActive(organizationId: string, isActive: boolean): Promise<void> {
  const token = await guard();
  await apiFetch(`/api/platform/brands/${organizationId}/${isActive ? "activate" : "deactivate"}`, {
    method: "POST",
    token,
  });
  revalidatePath("/admin/brands");
}

export async function enterBrand(formData: FormData): Promise<void> {
  const token = await guard();
  const organizationId = String(formData.get("organization_id") ?? "");
  if (!organizationId) redirect("/admin/brands");
  await apiFetch(`/api/platform/brands/${organizationId}/enter`, { method: "POST", token });
  await setAdminViewOrg(organizationId);
  // The admin-view banner lives in the root layout, outside this page's own Suspense boundary --
  // without this, Cache Components can keep serving its previously-rendered (wrong-brand) output
  // across this navigation since nothing else here tells it the cookie it reads has changed.
  revalidatePath("/", "layout");
  redirect("/dashboard");
}

export async function exitBrand(formData: FormData): Promise<void> {
  const token = await guard();
  const organizationId = String(formData.get("organization_id") ?? "");
  if (organizationId) {
    try {
      await apiFetch(`/api/platform/brands/${organizationId}/exit`, { method: "POST", token });
    } catch (e) {
      if (!(e instanceof ApiError)) throw e; // best-effort audit; still clear the view locally
    }
  }
  await clearAdminViewOrg();
  revalidatePath("/", "layout");
  redirect("/admin/brands");
}
