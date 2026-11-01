import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { PriorityBadge } from "@/components/priority-badge";
import { StatusBadge } from "@/components/status-badge";
import { getCurrentOrganization, getCurrentUser, getSupportAnalytics, getSupportConversations } from "@/lib/dal";
import { formatDateTime } from "@/lib/format";

export const metadata = { title: "Support · BizFlow AI" };

type SearchParams = Promise<{
  channel?: string;
  priority?: string;
  status?: string;
  assignment?: string;
}>;

const CHANNELS = ["All", "WEBSITE", "WHATSAPP", "EMAIL"];
const PRIORITIES = ["All", "URGENT", "HIGH", "NORMAL", "LOW"];
const STATUSES = ["All", "OPEN", "AI_ACTIVE", "HUMAN_REQUIRED", "HUMAN_ACTIVE", "WAITING", "RESOLVED", "CLOSED"];
const ASSIGNMENTS = [
  { value: "all", label: "All" },
  { value: "me", label: "Me" },
  { value: "unassigned", label: "Unassigned" },
];

export default function SupportPage({ searchParams }: { searchParams: SearchParams }) {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading support inbox…</p>}>
      <Support searchParams={searchParams} />
    </Suspense>
  );
}

async function Support({ searchParams }: { searchParams: SearchParams }) {
  const { channel, priority, status, assignment } = await searchParams;
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");

  const params = new URLSearchParams();
  if (channel && channel !== "All") params.set("channel", channel);
  if (priority && priority !== "All") params.set("priority", priority);
  if (status && status !== "All") params.set("status_filter", status);
  if (assignment && assignment !== "all") params.set("assignment", assignment);
  const query = params.toString() ? `?${params.toString()}` : "";

  const [conversations, analytics] = await Promise.all([
    getSupportConversations(query),
    getSupportAnalytics(),
  ]);

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 space-y-8 p-8">
      <AppHeader current="support" />
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Support</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Every WhatsApp, website and email conversation in one inbox.
        </p>
      </div>

      {analytics ? (
        <section
          aria-label="Analytics"
          data-testid="support-analytics"
          className="grid grid-cols-2 gap-3 sm:grid-cols-4"
        >
          <Stat label="Open" value={analytics.open_conversations} />
          <Stat label="Urgent" value={analytics.urgent_conversations} />
          <Stat label="AI handled" value={analytics.ai_handled} />
          <Stat label="Human handled" value={analytics.human_handled} />
        </section>
      ) : null}

      <section aria-label="Filters" className="flex flex-wrap gap-4 text-sm">
        <FilterGroup name="channel" current={channel ?? "All"} options={CHANNELS} base={{ priority, status, assignment }} />
        <FilterGroup name="priority" current={priority ?? "All"} options={PRIORITIES} base={{ channel, status, assignment }} />
        <FilterGroup name="status" current={status ?? "All"} options={STATUSES} base={{ channel, priority, assignment }} />
        <FilterGroup
          name="assignment"
          current={assignment ?? "all"}
          options={ASSIGNMENTS.map((a) => a.value)}
          labels={Object.fromEntries(ASSIGNMENTS.map((a) => [a.value, a.label]))}
          base={{ channel, priority, status }}
        />
      </section>

      <section aria-label="Conversations" className="space-y-2">
        <ul className="space-y-2">
          {conversations.map((conversation) => (
            <li
              key={conversation.id}
              data-conversation={conversation.id}
              data-channel={conversation.channel}
              data-status={conversation.status}
              className={`rounded-lg border p-3 ${
                conversation.unread
                  ? "border-blue-300 bg-blue-50/50 dark:border-blue-900 dark:bg-blue-950/30"
                  : "border-zinc-200 dark:border-zinc-800"
              }`}
            >
              <Link
                href={`/support/conversations/${conversation.id}`}
                className="flex flex-wrap items-center justify-between gap-2 text-sm"
              >
                <span className="min-w-0 flex-1">
                  <span className="font-medium">
                    {conversation.customer?.first_name || conversation.customer?.email || conversation.customer?.phone || "Unknown customer"}
                  </span>{" "}
                  <span className="text-zinc-500">
                    {conversation.last_message_preview || "(no messages yet)"}
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-1.5">
                  <StatusBadge status={conversation.channel} />
                  <PriorityBadge priority={conversation.priority} />
                  <StatusBadge status={conversation.status} />
                  {conversation.last_response_sender_type ? (
                    <StatusBadge status={conversation.last_response_sender_type} />
                  ) : null}
                  <span className="text-xs text-zinc-500">{formatDateTime(conversation.updated_at)}</span>
                </span>
              </Link>
            </li>
          ))}
          {conversations.length === 0 ? (
            <li className="text-sm text-zinc-500">No conversations match these filters.</li>
          ) : null}
        </ul>
      </section>
    </main>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border border-zinc-200 p-3 dark:border-zinc-800">
      <p className="text-2xl font-semibold">{value}</p>
      <p className="text-xs text-zinc-500">{label}</p>
    </div>
  );
}

function FilterGroup({
  name,
  current,
  options,
  labels,
  base,
}: {
  name: string;
  current: string;
  options: string[];
  labels?: Record<string, string>;
  base: Record<string, string | undefined>;
}) {
  return (
    <div className="flex items-center gap-1" role="group" aria-label={name}>
      <span className="text-xs uppercase text-zinc-500">{name}:</span>
      {options.map((option) => {
        const params = new URLSearchParams();
        for (const [key, value] of Object.entries(base)) if (value) params.set(key, value);
        if (option !== "All" && option !== "all") params.set(name, option);
        const href = `/support${params.toString() ? `?${params.toString()}` : ""}`;
        const active = option === current;
        return (
          <Link
            key={option}
            href={href}
            data-filter={`${name}-${option}`}
            className={`rounded-md px-2 py-1 text-xs ${
              active
                ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-900"
            }`}
          >
            {labels?.[option] ?? option}
          </Link>
        );
      })}
    </div>
  );
}
