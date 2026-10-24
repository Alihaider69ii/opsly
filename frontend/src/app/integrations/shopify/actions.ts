"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { ApiError, apiFetch } from "@/lib/api";
import { failure, text } from "@/lib/form";
import { clearSession, getToken } from "@/lib/session";
import { safeAuthorizationUrl } from "@/lib/shopify";
import type { FormState, ShopifyStatus } from "@/lib/types";

// Server Actions are public endpoints: every one re-reads the session itself and lets the backend
// authorize the action (role checks and tenancy are enforced there, never in the UI). The browser
// never talks to Shopify's API or ours with a credential: this file is the only bridge.

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

export async function connectShopify(_prev: FormState, formData: FormData): Promise<FormState> {
  const shop = text(formData, "shop");
  if (!shop) {
    return {
      error: "Please check the highlighted fields.",
      fieldErrors: { shop: "Enter your store address, like your-store.myshopify.com" },
      values: { shop },
    };
  }
  const result = await guard(
    (token) =>
      apiFetch<{ authorization_url: string }>("/api/integrations/shopify/connect", {
        method: "POST",
        token,
        body: { shop },
      }),
    { shop },
  );
  if (!result.ok) {
    // The shop address is the only field, so a validation message belongs under it.
    if (result.error instanceof ApiError && result.error.status === 422) {
      return {
        error: "Please check the highlighted fields.",
        fieldErrors: { shop: result.error.message },
        values: { shop },
      };
    }
    return result.state;
  }
  const url = safeAuthorizationUrl(result.data.authorization_url, {
    allowLocal: process.env.NODE_ENV !== "production",
  });
  if (!url)
    return { error: "Shopify returned an unexpected address. Please try again.", values: { shop } };
  redirect(url); // to Shopify, where the merchant approves the app; Shopify returns to /callback
}

export async function syncNow(): Promise<FormState> {
  const result = await guard((token) =>
    apiFetch<ShopifyStatus>("/api/integrations/shopify/sync", { method: "POST", token }),
  );
  if (!result.ok) return result.state;
  revalidatePath("/integrations/shopify");
  return { message: "Sync started. Counts update as data arrives." };
}

export async function disconnectShopify(): Promise<FormState> {
  const result = await guard((token) =>
    apiFetch<ShopifyStatus>("/api/integrations/shopify/disconnect", { method: "POST", token }),
  );
  if (!result.ok) return result.state;
  revalidatePath("/integrations/shopify");
  redirect("/integrations/shopify?disconnected=1"); // this form disappears, so say it on the page
}
