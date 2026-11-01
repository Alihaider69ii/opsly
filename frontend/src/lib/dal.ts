import "server-only";
import { cache } from "react";
import { redirect } from "next/navigation";
import { ApiError, apiFetch } from "./api";
import { getAdminViewOrg, getToken } from "./session";
import type {
  Brand,
  ChatWidget,
  DocumentDetail,
  DocumentPage,
  EmailCampaign,
  EmailAutomationUsage,
  EmailStatus,
  EmailTemplate,
  InventoryRow,
  KnowledgeSource,
  Organization,
  OperationalAction,
  OperationalIssue,
  OperationalTask,
  OperationsAnalytics,
  OperationsDashboard,
  OrderNeedingAttention,
  PaymentOrderRow,
  PlatformAuditEvent,
  PlatformDashboard,
  ReturnCaseRow,
  ShipmentRow,
  ShopifyStatus,
  AutomationWorkflow,
  SupportAnalytics,
  SupportConversationSummary,
  SupportMessage,
  SupportTimelineEvent,
  User,
  WhatsAppAutomationUsage,
  WhatsAppConsentList,
  WhatsAppConversation,
  WhatsAppMessage,
  WhatsAppStatus,
  WhatsAppTemplate,
} from "./types";

// Data Access Layer: the only place pages read the session. Every call is verified by the
// backend (the cookie's mere presence proves nothing), and wrapped in React cache() so a
// request that asks several times hits the API once.

async function authed<T>(path: string): Promise<T | null> {
  const token = await getToken();
  if (!token) return null;
  const viewOrgId = await getAdminViewOrg();
  try {
    return await apiFetch<T>(path, { token, viewOrgId });
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
  const viewOrgId = await getAdminViewOrg();
  try {
    return await apiFetch<T>(path, { token, viewOrgId });
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

export async function requirePlatformAdmin(): Promise<User> {
  const user = await getCurrentUser();
  if (!user) redirect("/admin/login");
  if (!user.is_platform_admin) redirect("/admin/login");
  return user;
}

export const getPlatformDashboard = cache(() => authed<PlatformDashboard>("/api/platform/dashboard"));

export async function getBrands(params?: {
  environment?: string;
  is_active?: boolean;
  search?: string;
}): Promise<Brand[]> {
  const query = new URLSearchParams();
  if (params?.environment) query.set("environment", params.environment);
  if (params?.is_active !== undefined) query.set("is_active", String(params.is_active));
  if (params?.search) query.set("search", params.search);
  const qs = query.toString();
  return (await authed<Brand[]>(`/api/platform/brands${qs ? `?${qs}` : ""}`)) ?? [];
}

export const getBrand = cache((id: string) => authedOrMissing<Brand>(`/api/platform/brands/${id}`));

export const getPlatformAudit = cache(
  async (organizationId?: string) =>
    (await authed<PlatformAuditEvent[]>(
      `/api/platform/audit${organizationId ? `?organization_id=${organizationId}` : ""}`,
    )) ?? [],
);

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

export const getAutomations = cache(() => authed<AutomationWorkflow[]>("/api/automations"));

export const getWhatsAppStatus = cache(() => authed<WhatsAppStatus>("/api/integrations/whatsapp"));

export const getWhatsAppConversations = cache(
  async () => (await authed<WhatsAppConversation[]>("/api/whatsapp/conversations")) ?? [],
);

export const getWhatsAppConversation = cache((id: string) =>
  authedOrMissing<WhatsAppConversation>(`/api/whatsapp/conversations/${id}`),
);

export const getWhatsAppMessages = cache(
  async (id: string) =>
    (await authedOrMissing<WhatsAppMessage[]>(`/api/whatsapp/conversations/${id}/messages`)) ??
    null,
);

export const getWhatsAppTemplates = cache(
  async () => (await authed<WhatsAppTemplate[]>("/api/whatsapp/templates")) ?? [],
);

export const getWhatsAppConsent = cache((contactId: string) =>
  authedOrMissing<WhatsAppConsentList>(`/api/whatsapp/contacts/${contactId}/consent`),
);

export const getWhatsAppAutomationUsage = cache(
  async () => (await authed<WhatsAppAutomationUsage[]>("/api/whatsapp/automation-usage")) ?? [],
);

// --- Phase 9: unified support inbox ------------------------------------------------------------

export const getSupportConversations = cache(
  async (query: string) =>
    (await authed<SupportConversationSummary[]>(`/api/support/conversations${query}`)) ?? [],
);

export const getSupportConversation = cache((id: string) =>
  authedOrMissing<SupportConversationSummary>(`/api/support/conversations/${id}`),
);

export const getSupportMessages = cache(
  async (id: string) => (await authedOrMissing<SupportMessage[]>(`/api/support/conversations/${id}/messages`)) ?? null,
);

export const getSupportConversationTimeline = cache(
  async (id: string) =>
    (await authedOrMissing<SupportTimelineEvent[]>(`/api/support/conversations/${id}/timeline`)) ?? null,
);

export const getSupportCustomerTimeline = cache(
  async (id: string) =>
    (await authedOrMissing<SupportTimelineEvent[]>(`/api/support/customers/${id}/timeline`)) ?? null,
);

export const getSupportAnalytics = cache(() => authed<SupportAnalytics>("/api/support/analytics"));

// --- Phase 9: email -----------------------------------------------------------------------------

export const getEmailStatus = cache(() => authed<EmailStatus>("/api/integrations/email"));

export const getEmailTemplates = cache(
  async () => (await authed<EmailTemplate[]>("/api/email/templates")) ?? [],
);

export const getEmailTemplate = cache((id: string) =>
  authedOrMissing<EmailTemplate>(`/api/email/templates/${id}`),
);

export const getEmailCampaigns = cache(
  async () => (await authed<EmailCampaign[]>("/api/email/campaigns")) ?? [],
);

export const getEmailCampaign = cache((id: string) =>
  authedOrMissing<EmailCampaign>(`/api/email/campaigns/${id}`),
);

export const getEmailAutomationUsage = cache(
  async () => (await authed<EmailAutomationUsage[]>("/api/email/automation-usage")) ?? [],
);

// --- Phase 10: D2C Operations AI ------------------------------------------------------------------

export const getOperationsDashboard = cache(() =>
  authed<OperationsDashboard>("/api/operations/dashboard"),
);

export const getOperationsAnalytics = cache(() =>
  authed<OperationsAnalytics>("/api/operations/analytics"),
);

export const getOperationalIssues = cache(
  async (query: string) => (await authed<OperationalIssue[]>(`/api/operations/issues${query}`)) ?? [],
);

export const getOperationalIssue = cache((id: string) =>
  authedOrMissing<OperationalIssue>(`/api/operations/issues/${id}`),
);

export const getOperationalTasks = cache(
  async (query: string) => (await authed<OperationalTask[]>(`/api/operations/tasks${query}`)) ?? [],
);

export const getOperationalTask = cache((id: string) =>
  authedOrMissing<OperationalTask>(`/api/operations/tasks/${id}`),
);

export const getOrdersNeedingAttention = cache(
  async () => (await authed<OrderNeedingAttention[]>("/api/operations/orders")) ?? [],
);

export const getInventoryOverview = cache(
  async () => (await authed<InventoryRow[]>("/api/operations/inventory")) ?? [],
);

export const getShipments = cache(
  async (query: string) => (await authed<ShipmentRow[]>(`/api/operations/shipping${query}`)) ?? [],
);

export const getPaymentOrders = cache(
  async () => (await authed<PaymentOrderRow[]>("/api/operations/payments")) ?? [],
);

export const getReturnCases = cache(
  async (query: string) => (await authed<ReturnCaseRow[]>(`/api/operations/returns${query}`)) ?? [],
);

export const getOperationalActions = cache(
  async () => (await authed<OperationalAction[]>("/api/operations/actions")) ?? [],
);

export const getOperationalAction = cache((id: string) =>
  authedOrMissing<OperationalAction>(`/api/operations/actions/${id}`),
);
