// Pure helpers for the Shopify integration page. No imports, so they run (and are unit-tested)
// outside Next.js. Nothing here touches a secret: the frontend never receives tokens at all.

const SHOPIFY_HOST_SUFFIX = ".myshopify.com";
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

// Where the merchant is sent to approve the app. The backend builds this URL (with the one-time
// `state`), but a server action that redirects to a URL it was handed should still verify it: only
// https://<store>.myshopify.com, or a plain-http local fake when developing.
export function safeAuthorizationUrl(raw: unknown, { allowLocal }: { allowLocal: boolean }) {
  if (typeof raw !== "string") return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.username || url.password) return null;
  const host = url.hostname.toLowerCase();
  const isShopify =
    url.protocol === "https:" && host.endsWith(SHOPIFY_HOST_SUFFIX) && host.length > 14;
  const isLocal = allowLocal && LOCAL_HOSTS.has(host) && ["http:", "https:"].includes(url.protocol);
  return isShopify || isLocal ? url.toString() : null;
}

// What the user is told for each code that can come back in `?error=`. The page only ever shows
// text from this table, so a crafted link cannot put arbitrary words on a trusted page.
export const SHOPIFY_NOTICES: Record<string, string> = {
  access_denied: "The connection was cancelled in Shopify. Nothing was changed.",
  invalid_state: "The Shopify authorization could not be verified. Please start again.",
  state_expired: "The Shopify authorization took too long. Please start again.",
  invalid_signature: "The Shopify authorization could not be verified. Please start again.",
  shop_mismatch: "The Shopify authorization was for a different store. Please start again.",
  scope_missing:
    "Shopify did not grant the permissions BizFlow needs. Please reconnect and approve the requested access.",
  exchange_failed: "Shopify did not complete the connection. Please try again.",
  already_connected: "That store is already connected to another BizFlow account.",
  different_store: "Disconnect your current store before connecting a different one.",
  not_configured: "Shopify is not configured on this server.",
  secrets_not_configured:
    "Secure storage is not configured on this server, so a store cannot be connected yet.",
};

export const GENERIC_NOTICE = "The Shopify connection could not be completed. Please try again.";

export function noticeFor(code: string | undefined): string {
  if (code === undefined) return GENERIC_NOTICE;
  return Object.hasOwn(SHOPIFY_NOTICES, code) ? SHOPIFY_NOTICES[code] : GENERIC_NOTICE;
}

// Shopify sends the merchant back with `?error=access_denied` when they decline. Everything else
// is forwarded to the backend untouched: its signature covers every parameter.
export function declinedInShopify(search: string): boolean {
  const params = new URLSearchParams(search);
  return params.get("error") === "access_denied" && !params.has("code");
}

const CODE_RE = /^[a-z_]{1,40}$/;

export function callbackRedirectPath(outcome: { ok: true } | { ok: false; code?: string }) {
  if (outcome.ok) return "/integrations/shopify?connected=1";
  const code = outcome.code && CODE_RE.test(outcome.code) ? outcome.code : "failed";
  return `/integrations/shopify?error=${code}`;
}

export const RESOURCE_LABELS: Record<string, string> = {
  PRODUCTS: "Products",
  CUSTOMERS: "Customers",
  ORDERS: "Orders",
};

export const WEBHOOK_LABELS: Record<string, string> = {
  REGISTERED: "Live updates are on",
  PARTIAL: "Live updates are partly on",
  FAILED: "Live updates could not be set up",
  NOT_CONFIGURED: "Live updates are off (no public webhook address configured)",
};

export function webhookLabel(status: string): string {
  return Object.hasOwn(WEBHOOK_LABELS, status) ? WEBHOOK_LABELS[status] : "Live updates: unknown";
}

export function isActiveSync(status: string | undefined): boolean {
  return status === "SYNCING";
}

export function scopeList(scopes: string[]): string {
  return scopes.length ? scopes.join(", ") : "none";
}
