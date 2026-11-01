export type User = {
  id: string;
  email: string;
  full_name: string | null;
  is_platform_admin: boolean;
  created_at: string;
};

export type Role = "OWNER" | "ADMIN" | "MEMBER";

export type BrandEnvironment = "DEMO" | "PRODUCTION";

export type Organization = {
  id: string;
  name: string;
  slug: string;
  website: string | null;
  business_type: string | null;
  environment: BrandEnvironment;
  is_active: boolean;
  created_at: string;
  role: Role;
};

export type TokenResponse = {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  user: User;
};

export type Brand = {
  id: string;
  name: string;
  slug: string;
  environment: BrandEnvironment;
  is_active: boolean;
  owner_email: string | null;
  user_count: number;
  customer_count: number;
  open_issues: number;
  open_tasks: number;
  created_at: string;
  last_activity_at: string | null;
};

export type BrandCreated = {
  brand: Brand;
  owner_email: string;
};

export type PlatformDashboard = {
  total_brands: number;
  active_brands: number;
  demo_brands: number;
  production_brands: number;
  total_users: number;
  total_customers: number;
  total_conversations: number;
  open_support_issues: number;
  open_operational_issues: number;
  open_operational_tasks: number;
  ai_queries_recent: number;
  connected_integrations: number;
};

export type PlatformAuditEvent = {
  id: string;
  actor_user_id: string;
  actor_email: string | null;
  event_type: string;
  organization_id: string | null;
  organization_name: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
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
  definition: {
    trigger: { event: string };
    conditions: { field: string; op: string; value?: unknown }[];
    actions: Record<string, unknown>[];
  };
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

export type WhatsAppConnectionStatus = "CONNECTED" | "ERROR" | "DISCONNECTED";

export type WhatsAppAIConfidence = "HIGH" | "MEDIUM" | "LOW" | "NONE";

// Everything the dashboard may know about the connection. The API never returns the access token.
export type WhatsAppConnection = {
  phone_number_id: string;
  waba_id: string;
  business_id: string | null;
  display_phone_number: string | null;
  verified_name: string | null;
  quality_rating: string | null;
  api_version: string;
  default_country: string | null;
  ai_auto_reply_enabled: boolean;
  ai_grounding_required: boolean;
  ai_min_confidence: WhatsAppAIConfidence;
  ai_handoff_enabled: boolean;
  ai_fallback_message: string | null;
  ai_handoff_message: string | null;
  ai_max_replies_per_window: number;
  status: WhatsAppConnectionStatus;
  connected_at: string;
  last_verified_at: string | null;
  error_code: string | null;
  error_message: string | null;
};

export type WhatsAppStatus = {
  available: boolean;
  secure_storage: boolean;
  connection: WhatsAppConnection | null;
};

export type WhatsAppAIState = "AI_ACTIVE" | "HUMAN_REQUIRED" | "HUMAN_ACTIVE" | "RESOLVED";

export type WhatsAppConversation = {
  id: string;
  contact_id: string;
  connection_id: string;
  status: "OPEN" | "CLOSED";
  last_inbound_at: string | null;
  last_outbound_at: string | null;
  window_expires_at: string | null;
  message_count: number;
  ai_state: WhatsAppAIState;
  handoff_reason: string | null;
  handoff_at: string | null;
  ai_replies_in_window: number;
  created_at: string;
  updated_at: string;
};

export type WhatsAppMessageStatus =
  "QUEUED" | "SENDING" | "SENT" | "DELIVERED" | "READ" | "FAILED" | "CANCELLED" | "RECEIVED";

export type WhatsAppMessageAI = {
  grounded: boolean;
  confidence: WhatsAppAIConfidence;
  retrieval_used: boolean;
  sources_count: number;
  sources: { url: string; title: string; score: number }[];
  model: string | null;
  latency_ms: number;
  prompt_version: string;
  prompt_tokens: number | null;
  completion_tokens: number | null;
};

export type WhatsAppMessagePolicy = {
  outcome: "sent" | "fallback" | "handoff";
  reason?: string;
};

export type WhatsAppMessage = {
  id: string;
  direction: "INBOUND" | "OUTBOUND";
  message_type: "TEXT" | "TEMPLATE" | "MEDIA";
  status: WhatsAppMessageStatus;
  content: string | null;
  template_name: string | null;
  template_language: string | null;
  error_code: string | null;
  ai: WhatsAppMessageAI | null;
  policy: WhatsAppMessagePolicy | null;
  created_at: string;
  sent_at: string | null;
  delivered_at: string | null;
  read_at: string | null;
  failed_at: string | null;
};

export type WhatsAppTemplate = {
  id: string;
  name: string;
  language: string;
  category: string;
  status: string;
  provider_template_id: string | null;
  components: Record<string, unknown>[];
  last_synced_at: string | null;
};

export type WhatsAppConsentCategory = "TRANSACTIONAL" | "MARKETING" | "SUPPORT";

export type WhatsAppConsent = {
  category: WhatsAppConsentCategory;
  status: "OPTED_IN" | "OPTED_OUT" | null; // null = UNKNOWN, never treated as opted in
};

export type WhatsAppConsentList = {
  contact_id: string;
  consents: WhatsAppConsent[];
  suppressed: boolean;
};

export type ConsentIn = {
  category: WhatsAppConsentCategory;
  status: "OPTED_IN" | "OPTED_OUT";
  source: string;
};

export type WhatsAppAutomationUsage = {
  workflow_id: string;
  name: string;
  status: string;
  whatsapp_actions: string[];
};

// --- Phase 9: unified support inbox -------------------------------------------------------------

export type SupportChannel = "WEBSITE" | "WHATSAPP" | "EMAIL";
export type SupportStatus =
  | "OPEN"
  | "AI_ACTIVE"
  | "HUMAN_REQUIRED"
  | "HUMAN_ACTIVE"
  | "WAITING"
  | "RESOLVED"
  | "CLOSED";
export type SupportPriority = "URGENT" | "HIGH" | "NORMAL" | "LOW";
export type AIHandoffState = "AI_ACTIVE" | "HUMAN_REQUIRED" | "HUMAN_ACTIVE" | "RESOLVED";
export type SenderType = "CUSTOMER" | "AI" | "HUMAN" | "SYSTEM";

export type CustomerSummary = {
  id: string;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  phone: string | null;
  shopify_customer_id: number | null;
  email_marketing_consent: boolean | null;
  email_suppressed: boolean;
};

export type SupportConversationSummary = {
  id: string;
  channel: SupportChannel;
  channel_conversation_id: string;
  customer_id: string | null;
  status: SupportStatus;
  priority: SupportPriority;
  ai_state: AIHandoffState;
  assigned_agent_user_id: string | null;
  last_customer_activity_at: string | null;
  last_response_at: string | null;
  last_response_sender_type: SenderType | null;
  last_message_preview: string | null;
  unread: boolean;
  created_at: string;
  updated_at: string;
  customer: CustomerSummary | null;
};

export type SupportMessage = {
  id: string;
  direction: "INBOUND" | "OUTBOUND";
  sender_type: SenderType | null;
  sender_user_id: string | null;
  content: string | null;
  status: string;
  ai: Record<string, unknown> | null;
  policy: Record<string, unknown> | null;
  error_code: string | null;
  created_at: string;
};

export type SupportTimelineEvent = {
  id: string;
  event_type: string;
  payload: Record<string, unknown>;
  support_conversation_id: string | null;
  created_at: string;
};

export type SupportActionResult = {
  ok: boolean;
  conversation: SupportConversationSummary;
};

export type SupportAnalytics = {
  total_conversations: number;
  open_conversations: number;
  urgent_conversations: number;
  high_priority_conversations: number;
  ai_handled: number;
  human_handled: number;
  ai_handoffs: number;
  avg_first_response_seconds: number | null;
  avg_resolution_seconds: number | null;
  emails_sent: number;
  emails_delivered: number;
  emails_bounced: number;
  emails_unsubscribed: number;
};

// --- Phase 9: email --------------------------------------------------------------------------

export type EmailConnection = {
  status: WhatsAppConnectionStatus;
  from_address: string;
  from_name: string | null;
  reply_to_address: string | null;
  inbound_address: string;
  enabled: boolean;
  ai_auto_reply_enabled: boolean;
  ai_grounding_required: boolean;
  ai_min_confidence: WhatsAppAIConfidence;
  ai_handoff_enabled: boolean;
  ai_fallback_message: string | null;
  ai_handoff_message: string | null;
  ai_max_replies_per_window: number;
  connected_at: string;
  error_code: string | null;
  error_message: string | null;
};

export type EmailStatus = {
  available: boolean;
  connection: EmailConnection | null;
};

export type EmailClassification = "TRANSACTIONAL" | "LIFECYCLE" | "MARKETING";
export type EmailTemplateStatus = "DRAFT" | "ACTIVE" | "ARCHIVED";

export type EmailTemplate = {
  id: string;
  name: string;
  type: EmailClassification;
  subject: string;
  preheader: string | null;
  body_html: string;
  body_text: string | null;
  variables: string[];
  status: EmailTemplateStatus;
  version: number;
  created_by: string | null;
  created_at: string;
  updated_at: string;
};

export type EmailCampaignStatus =
  | "DRAFT"
  | "SCHEDULED"
  | "RUNNING"
  | "PAUSED"
  | "COMPLETED"
  | "CANCELLED";

export type EmailCampaign = {
  id: string;
  name: string;
  template_id: string;
  audience_definition: Record<string, unknown>;
  status: EmailCampaignStatus;
  scheduled_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_by: string | null;
  total_recipients: number;
  sent_count: number;
  failed_count: number;
  skipped_count: number;
  created_at: string;
  updated_at: string;
};

export type EmailAutomationUsage = {
  workflow_id: string;
  name: string;
  status: string;
  email_actions: string[];
};

// --- Phase 10: D2C Operations AI --------------------------------------------------------------

export type IssueCategory =
  | "ORDER"
  | "SHIPPING"
  | "DELIVERY"
  | "NDR"
  | "RTO"
  | "RETURN"
  | "REFUND"
  | "EXCHANGE"
  | "INVENTORY"
  | "WAREHOUSE"
  | "PAYMENT"
  | "FRAUD"
  | "PRODUCT"
  | "PROCUREMENT"
  | "CUSTOMER"
  | "SLA"
  | "RECONCILIATION"
  | "OTHER";

export type OperationsPriority = "URGENT" | "HIGH" | "NORMAL" | "LOW";
export type IssueSeverity = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";
export type IssueStatus =
  | "OPEN"
  | "ACKNOWLEDGED"
  | "IN_PROGRESS"
  | "WAITING"
  | "ESCALATED"
  | "RESOLVED"
  | "DISMISSED";
export type TaskStatus = "OPEN" | "ASSIGNED" | "IN_PROGRESS" | "WAITING" | "ESCALATED" | "RESOLVED" | "CANCELLED";
export type DetectedBy = "RULE" | "AI" | "HUMAN" | "INTEGRATION";
export type ActionCategory = "SAFE_AUTOMATION" | "REQUIRES_APPROVAL" | "FORBIDDEN";
export type ActionStatus = "PENDING" | "APPROVED" | "REJECTED" | "EXECUTED" | "FAILED";

export type OperationalIssue = {
  id: string;
  customer_id: string | null;
  order_id: string | null;
  category: IssueCategory;
  subcategory: string | null;
  priority: OperationsPriority;
  priority_source: "SYSTEM" | "AI" | "HUMAN";
  severity: IssueSeverity;
  status: IssueStatus;
  title: string;
  description: string | null;
  evidence: Record<string, unknown>;
  detected_by: DetectedBy;
  detector_code: string | null;
  recommended_action: string | null;
  assigned_user_id: string | null;
  sla_id: string | null;
  due_at: string | null;
  breached_at: string | null;
  resolved_at: string | null;
  created_at: string;
  updated_at: string;
  audit?: OperationalAuditEvent[];
  tasks?: OperationalTask[];
};

export type OperationalTask = {
  id: string;
  issue_id: string | null;
  customer_id: string | null;
  order_id: string | null;
  title: string;
  description: string | null;
  category: IssueCategory;
  priority: OperationsPriority;
  status: TaskStatus;
  assigned_user_id: string | null;
  created_by_type: DetectedBy;
  due_at: string | null;
  completed_at: string | null;
  resolution: string | null;
  created_at: string;
  updated_at: string;
};

export type OperationalAction = {
  id: string;
  action_type: string;
  category: ActionCategory;
  resource_type: string;
  resource_id: string | null;
  status: ActionStatus;
  payload: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  error_code: string | null;
  executed_at: string | null;
  created_at: string;
};

export type OperationalAuditEvent = {
  id: string;
  actor_type: DetectedBy;
  actor_id: string | null;
  event_type: string;
  resource_type: string;
  resource_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type OrderNeedingAttention = {
  order_id: string;
  order_number: string | null;
  total: string | null;
  financial_status: string | null;
  fulfillment_status: string | null;
  issue_id: string;
  issue_category: IssueCategory;
  issue_priority: OperationsPriority;
  issue_title: string;
};

export type InventoryRow = {
  variant_id: string;
  product_id: string;
  title: string;
  sku: string | null;
  inventory_quantity: number | null;
  out_of_stock: boolean;
  low_stock: boolean;
};

export type ShipmentRow = {
  id: string;
  order_id: string;
  carrier: string | null;
  tracking_number: string | null;
  status: string;
  shipped_at: string | null;
  estimated_delivery_at: string | null;
  delivered_at: string | null;
  ndr_count: number;
};

export type PaymentOrderRow = {
  order_id: string;
  order_number: string | null;
  financial_status: string | null;
  total: string | null;
};

export type ReturnCaseRow = {
  id: string;
  order_id: string;
  customer_id: string | null;
  type: string;
  status: string;
  requested_at: string;
  resolved_at: string | null;
};

export type OperationsDashboard = {
  kpis: Record<string, number>;
  urgent_queue: OperationalIssue[];
  sla_breaches: OperationalIssue[];
  orders_needing_attention: OrderNeedingAttention[];
  inventory_alerts: InventoryRow[];
  open_tasks: OperationalTask[];
  recent_activity: OperationalAuditEvent[];
};

export type OperationsAnalytics = {
  kpis: Record<string, number>;
  issues_by_category: Record<string, number>;
  issues_by_priority: Record<string, number>;
  issues_by_status: Record<string, number>;
  tasks_by_status: Record<string, number>;
  ai_vs_human: { issues_detected_by: Record<string, number>; tasks_created_by: Record<string, number> };
  average_resolution_minutes: number | null;
  actions_summary: Record<string, number>;
};

export type AIQueryResult = {
  summary: string;
  evidence: Record<string, unknown>[];
  reasoning_summary: string;
  recommendation: string | null;
  risk: string | null;
  required_approval: boolean;
  suggested_action: Record<string, unknown> | null;
  grounded: boolean;
};

// State returned by form Server Actions (consumed with useActionState). Never contains passwords.
export type FormState = {
  error?: string;
  message?: string;
  fieldErrors?: Record<string, string>;
  values?: Record<string, string>;
} | null;
