// Deterministic formatting: output must not depend on the server's or browser's locale or time
// zone, so server-rendered and client-rendered text can never disagree.

export function formatDateTime(iso: string | null): string {
  if (!iso) return "never";
  const date = new Date(iso.endsWith("Z") || /[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(date.getTime())) return "unknown";
  return `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function hostOf(url: string | null): string {
  if (!url) return "";
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export function isInProgress(status: string): boolean {
  return status === "PENDING" || status === "CRAWLING";
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: string): boolean {
  return UUID_RE.test(value);
}

// Plain-language one-liner for the last crawl; every number comes straight from the backend.
export function crawlSummary(stats: Record<string, unknown> | null): string | null {
  if (!stats) return null;
  const n = (key: string) => (typeof stats[key] === "number" ? (stats[key] as number) : 0);
  const parts = [`${n("documents")} pages indexed`, `${n("chunks_created")} new chunks`];
  if (n("skipped_robots")) parts.push(`${n("skipped_robots")} blocked by robots.txt`);
  if (n("failed")) parts.push(`${n("failed")} failed`);
  if (stats.stopped_by === "page_limit") parts.push("stopped at the page limit");
  if (stats.stopped_by === "time_limit") parts.push("stopped at the time limit");
  return parts.join(" · ");
}
