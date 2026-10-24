export type User = {
  id: string;
  email: string;
  full_name: string | null;
  created_at: string;
};

export type Role = "OWNER" | "ADMIN" | "MEMBER";

export type Organization = {
  id: string;
  name: string;
  slug: string;
  website: string | null;
  business_type: string | null;
  created_at: string;
  role: Role;
};

export type TokenResponse = {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  user: User;
};

export type SourceStatus = "PENDING" | "CRAWLING" | "COMPLETED" | "FAILED";

export type CrawlStats = {
  pages_fetched?: number;
  pages_extracted?: number;
  skipped_robots?: number;
  skipped_non_html?: number;
  skipped_thin?: number;
  skipped_duplicate?: number;
  skipped_off_site?: number;
  failed?: number;
  stopped_by?: "page_limit" | "time_limit" | null;
  documents?: number;
  chunks_created?: number;
  documents_removed?: number;
};

export type KnowledgeSource = {
  id: string;
  type: string;
  name: string;
  url: string | null;
  status: SourceStatus;
  document_count: number;
  last_crawled_at: string | null;
  crawl_started_at: string | null;
  error_code: string | null;
  error_message: string | null;
  crawl_stats: CrawlStats | null;
  created_at: string;
  updated_at: string;
};

export type KnowledgeDocument = {
  id: string;
  url: string;
  title: string | null;
  status: "PENDING" | "READY" | "FAILED";
  character_count: number;
  chunk_count: number;
  content_preview: string;
  updated_at: string;
};

export type DocumentPage = {
  items: KnowledgeDocument[];
  total: number;
  limit: number;
  offset: number;
};

export type DocumentDetail = Omit<KnowledgeDocument, "content_preview"> & { content: string };

export type ChatWidget = {
  public_key: string;
  enabled: boolean;
  welcome_message: string;
  allowed_origins: string[];
};

export type ShopifyConnectionStatus = "CONNECTED" | "SYNCING" | "ERROR" | "DISCONNECTED";

export type ShopifySyncStatus = "IDLE" | "PENDING" | "RUNNING" | "COMPLETED" | "FAILED" | "SKIPPED";

export type ShopifyConnection = {
  shop_domain: string;
  shop_name: string | null;
  currency: string | null;
  timezone: string | null;
  api_version: string;
  status: ShopifyConnectionStatus;
  installed_at: string;
  last_sync_at: string | null;
  webhook_status: "NOT_CONFIGURED" | "REGISTERED" | "PARTIAL" | "FAILED";
  error_code: string | null;
  error_message: string | null;
};

export type ShopifySyncStage = {
  resource: "PRODUCTS" | "CUSTOMERS" | "ORDERS";
  status: ShopifySyncStatus;
  items_synced: number;
  items_skipped: number;
  started_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  error_message: string | null;
};

// Everything the dashboard may know about the store. The API never returns tokens, secrets or
// customer personal data, so there is nothing of the sort to render.
export type ShopifyStatus = {
  available: boolean;
  secure_storage: boolean;
  webhooks_configured: boolean;
  connection: ShopifyConnection | null;
  scopes: string[];
  sync: ShopifySyncStage[];
  counts: { products: number; variants: number; customers: number; orders: number };
};

export type AutomationWorkflow = {
  id: string;
  name: string;
  description: string | null;
  status: "DRAFT" | "ACTIVE" | "PAUSED" | "ARCHIVED";
  version: number;
  definition: { trigger: { event: string }; conditions: { field: string; op: string; value?: unknown }[]; actions: Record<string, unknown>[] };
  created_at: string;
  updated_at: string;
  recent_executions?: AutomationExecution[];
};

export type AutomationExecution = {
  id: string;
  workflow_id: string;
  event_id: string;
  workflow_version: number;
  status: string;
  current_step: number;
  attempts: number;
  error_code: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

// State returned by form Server Actions (consumed with useActionState). Never contains passwords.
export type FormState = {
  error?: string;
  message?: string;
  fieldErrors?: Record<string, string>;
  values?: Record<string, string>;
} | null;
