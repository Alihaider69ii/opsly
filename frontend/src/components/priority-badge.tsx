// A dedicated badge for support priority (URGENT/HIGH/NORMAL/LOW): kept separate from
// StatusBadge because "HIGH"/"LOW"/"NONE" already mean something different there (WhatsApp AI
// confidence) -- reusing the same lookup table would show the wrong color/label for priority.

const styles: Record<string, string> = {
  URGENT: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  HIGH: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  NORMAL: "bg-zinc-100 text-zinc-700 dark:bg-zinc-900 dark:text-zinc-300",
  LOW: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
};

const labels: Record<string, string> = {
  URGENT: "Urgent",
  HIGH: "High",
  NORMAL: "Normal",
  LOW: "Low",
};

export function PriorityBadge({ priority }: { priority: string }) {
  return (
    <span
      data-priority={priority}
      className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-medium ${styles[priority] ?? ""}`}
    >
      {labels[priority] ?? priority}
    </span>
  );
}
