import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { PriorityBadge } from "@/components/priority-badge";
import { StatusBadge } from "@/components/status-badge";
import { getCurrentOrganization, getCurrentUser, getOperationsDashboard } from "@/lib/dal";
import { formatDateTime } from "@/lib/format";
import { SweepButton } from "./sweep-button";

export const metadata = { title: "Operations · BizFlow AI" };

export default function OperationsPage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading operations…</p>}>
      <Operations />
    </Suspense>
  );
}

const KPI_LABELS: Record<string, string> = {
  open_issues: "Open Issues",
  urgent_issues: "Urgent Issues",
  high_priority_issues: "High Priority",
  sla_breaches: "SLA Breaches",
  open_tasks: "Open Tasks",
  orders_needing_attention: "Orders Needing Attention",
  inventory_alerts: "Inventory Alerts",
  payment_alerts: "Payment Alerts",
  shipping_ndr_rto_alerts: "Shipping / NDR / RTO",
};

async function Operations() {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");

  const dashboard = await getOperationsDashboard();

  return (
    <main className="mx-auto w-full max-w-5xl flex-1 space-y-8 p-8">
      <AppHeader current="operations" />
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Operations</h1>
          <p className="mt-1 text-sm text-zinc-500">
            The operating brain of the business: detected problems, prioritized, with evidence and
            recommended actions.
          </p>
        </div>
        <SweepButton />
      </div>

      {dashboard ? (
        <>
          <section aria-label="KPIs" data-testid="operations-kpis" className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {Object.entries(dashboard.kpis).map(([key, value]) => (
              <Stat key={key} label={KPI_LABELS[key] ?? key} value={value} />
            ))}
          </section>

          <nav aria-label="Operations sections" className="flex flex-wrap gap-2 text-sm">
            {[
              ["/operations/tasks", "Tasks"],
              ["/operations/orders", "Orders"],
              ["/operations/inventory", "Inventory"],
              ["/operations/shipping", "Shipping / NDR / RTO"],
              ["/operations/payments", "Payments"],
              ["/operations/analytics", "Analytics"],
              ["/operations/ai", "Ask Operations AI"],
            ].map(([href, label]) => (
              <Link
                key={href}
                href={href}
                className="rounded-md border border-zinc-300 px-3 py-1.5 hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-900"
              >
                {label}
              </Link>
            ))}
          </nav>

          <Section title="Urgent Queue" testId="urgent-queue">
            <IssueList issues={dashboard.urgent_queue} empty="No urgent issues right now." />
          </Section>

          <Section title="SLA Breaches" testId="sla-breaches">
            <IssueList issues={dashboard.sla_breaches} empty="No SLA breaches right now." />
          </Section>

          <Section title="Orders Needing Attention" testId="orders-needing-attention">
            <ul className="space-y-2">
              {dashboard.orders_needing_attention.map((row) => (
                <li
                  key={row.issue_id}
                  data-order={row.order_id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-zinc-200 p-3 text-sm dark:border-zinc-800"
                >
                  <span>
                    <span className="font-medium">{row.order_number ?? row.order_id}</span>{" "}
                    <span className="text-zinc-500">{row.issue_title}</span>
                  </span>
                  <span className="flex items-center gap-1.5">
                    <StatusBadge status={row.issue_category} />
                    <PriorityBadge priority={row.issue_priority} />
                  </span>
                </li>
              ))}
              {dashboard.orders_needing_attention.length === 0 ? (
                <li className="text-sm text-zinc-500">No orders currently need attention.</li>
              ) : null}
            </ul>
          </Section>

          <Section title="Inventory Alerts" testId="inventory-alerts">
            <ul className="space-y-2">
              {dashboard.inventory_alerts.map((row) => (
                <li
                  key={row.variant_id}
                  className="flex items-center justify-between gap-2 rounded-lg border border-zinc-200 p-3 text-sm dark:border-zinc-800"
                >
                  <span>
                    {row.title} {row.sku ? <span className="text-zinc-500">({row.sku})</span> : null}
                  </span>
                  <StatusBadge status={row.out_of_stock ? "FAILED" : "WAITING"} />
                </li>
              ))}
              {dashboard.inventory_alerts.length === 0 ? (
                <li className="text-sm text-zinc-500">No inventory alerts right now.</li>
              ) : null}
            </ul>
          </Section>

          <Section title="Open Tasks" testId="open-tasks">
            <ul className="space-y-2">
              {dashboard.open_tasks.map((task) => (
                <li
                  key={task.id}
                  className="flex items-center justify-between gap-2 rounded-lg border border-zinc-200 p-3 text-sm dark:border-zinc-800"
                >
                  <span>{task.title}</span>
                  <span className="flex items-center gap-1.5">
                    <StatusBadge status={task.status} />
                    <PriorityBadge priority={task.priority} />
                  </span>
                </li>
              ))}
              {dashboard.open_tasks.length === 0 ? (
                <li className="text-sm text-zinc-500">No open tasks.</li>
              ) : null}
            </ul>
          </Section>

          <Section title="Recent Operational Activity" testId="recent-activity">
            <ul className="space-y-1">
              {dashboard.recent_activity.map((event) => (
                <li key={event.id} className="text-xs text-zinc-500">
                  {formatDateTime(event.created_at)} · {event.event_type} · {event.resource_type}
                  {" · "}
                  <StatusBadge status={event.actor_type} />
                </li>
              ))}
              {dashboard.recent_activity.length === 0 ? (
                <li className="text-sm text-zinc-500">No activity yet.</li>
              ) : null}
            </ul>
          </Section>
        </>
      ) : (
        <p className="text-sm text-zinc-500">Could not load the operations dashboard.</p>
      )}
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

function Section({
  title,
  testId,
  children,
}: {
  title: string;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <section aria-label={title} data-testid={testId} className="space-y-2">
      <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
      {children}
    </section>
  );
}

function IssueList({
  issues,
  empty,
}: {
  issues: { id: string; title: string; category: string; priority: string; severity: string; due_at: string | null }[];
  empty: string;
}) {
  return (
    <ul className="space-y-2">
      {issues.map((issue) => (
        <li key={issue.id} data-issue={issue.id}>
          <Link
            href={`/operations/issues/${issue.id}`}
            className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-zinc-200 p-3 text-sm hover:bg-zinc-50 dark:border-zinc-800 dark:hover:bg-zinc-900"
          >
            <span className="min-w-0 flex-1">{issue.title}</span>
            <span className="flex shrink-0 items-center gap-1.5">
              <StatusBadge status={issue.category} />
              <PriorityBadge priority={issue.priority} />
              {issue.due_at ? <span className="text-xs text-zinc-500">due {formatDateTime(issue.due_at)}</span> : null}
            </span>
          </Link>
        </li>
      ))}
      {issues.length === 0 ? <li className="text-sm text-zinc-500">{empty}</li> : null}
    </ul>
  );
}
