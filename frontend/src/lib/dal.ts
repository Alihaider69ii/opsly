import "server-only";
import { cache } from "react";
import { redirect } from "next/navigation";
import { ApiError, apiFetch } from "./api";
import { getToken } from "./session";
import type {
  ChatWidget,
  DocumentDetail,
  DocumentPage,
  KnowledgeSource,
  Organization,
  ShopifyStatus,
  User,
} from "./types";

// Data Access Layer: the only place pages read the session. Every call is verified by the
// backend (the cookie's mere presence proves nothing), and wrapped in React cache() so a
// request that asks several times hits the API once.

async function authed<T>(path: string): Promise<T | null> {
  const token = await getToken();
  if (!token) return null;
  try {
    return await apiFetch<T>(path, { token });
  } catch (e) {
    // 401 = expired/invalid token; 403 = valid user with no organization yet.
    if (e instanceof ApiError && (e.status === 401 || e.status === 403)) return null;
    throw e;
  }
}

// Like authed(), but a 404 (missing, or belonging to another organization: the API makes them
// indistinguishable) also becomes null. Callers decide between login redirect and notFound().
async function authedOrMissing<T>(path: string): Promise<T | null> {
  const token = await getToken();
  if (!token) return null;
  try {
    return await apiFetch<T>(path, { token });
  } catch (e) {
    if (e instanceof ApiError && [401, 403, 404].includes(e.status)) return null;
    throw e;
  }
}

export const getCurrentUser = cache(() => authed<User>("/api/auth/me"));

export const getOrganizations = cache(
  async () => (await authed<Organization[]>("/api/organizations")) ?? [],
);

export const getCurrentOrganization = cache(() =>
  authed<Organization>("/api/organizations/current"),
);

export async function requireUser(): Promise<User> {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  return user;
}

export const getKnowledgeSources = cache(
  async () => (await authed<KnowledgeSource[]>("/api/knowledge/sources")) ?? [],
);

export const getKnowledgeSource = cache((id: string) =>
  authedOrMissing<KnowledgeSource>(`/api/knowledge/sources/${id}`),
);

export const getSourceDocuments = cache((id: string, limit: number, offset: number) =>
  authedOrMissing<DocumentPage>(
    `/api/knowledge/sources/${id}/documents?limit=${limit}&offset=${offset}`,
  ),
);

export const getSourceDocument = cache((id: string, documentId: string) =>
  authedOrMissing<DocumentDetail>(`/api/knowledge/sources/${id}/documents/${documentId}`),
);

export const getChatWidget = cache(() => authed<ChatWidget>("/api/chat/widget"));

export const getShopifyStatus = cache(() => authed<ShopifyStatus>("/api/integrations/shopify"));
