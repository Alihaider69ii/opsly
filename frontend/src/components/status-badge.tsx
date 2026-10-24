const styles: Record<string, string> = {
  PENDING: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  CRAWLING: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
  COMPLETED: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300",
  READY: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300",
  FAILED: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  CONNECTED: "bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300",
  SYNCING: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
  RUNNING: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
  ERROR: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  DISCONNECTED: "bg-zinc-100 text-zinc-700 dark:bg-zinc-900 dark:text-zinc-300",
  SKIPPED: "bg-zinc-100 text-zinc-700 dark:bg-zinc-900 dark:text-zinc-300",
  IDLE: "bg-zinc-100 text-zinc-700 dark:bg-zinc-900 dark:text-zinc-300",
};

const labels: Record<string, string> = {
  PENDING: "Pending",
  CRAWLING: "Crawling",
  COMPLETED: "Completed",
  READY: "Ready",
  FAILED: "Failed",
  CONNECTED: "Connected",
  SYNCING: "Syncing",
  RUNNING: "Running",
  ERROR: "Needs attention",
  DISCONNECTED: "Disconnected",
  SKIPPED: "Skipped",
  IDLE: "Idle",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      data-status={status}
      className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-medium ${styles[status] ?? ""}`}
    >
      {labels[status] ?? status}
    </span>
  );
}
