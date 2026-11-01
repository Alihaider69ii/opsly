import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { getCurrentOrganization, getCurrentUser, getOperationsAnalytics } from "@/lib/dal";

export const metadata = { title: "Analytics · Operations · BizFlow AI" };

export default function AnalyticsPage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading analytics…</p>}>
      <Analytics />
    </Suspense>
  );
}

async function Analytics() {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");

  const analytics = await getOperationsAnalytics();

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 space-y-8 p-8">
      <AppHeader current="operations" />
      <div>
        <p className="text-sm text-zinc-500">
          <Link href="/operations" className="hover:underline">
            Operations
          </Link>{" "}
          · Analytics
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">Operations Analytics</h1>
      </div>

      {analytics ? (
        <div className="grid gap-6 sm:grid-cols-2" data-testid="operations-analytics">
          <Breakdown title="Issues by category" data={analytics.issues_by_category} />
          <Breakdown title="Issues by priority" data={analytics.issues_by_priority} />
          <Breakdown title="Issues by status" data={analytics.issues_by_status} />
          <Breakdown title="Tasks by status" data={analytics.tasks_by_status} />
          <Breakdown title="Issues detected by" data={analytics.ai_vs_human.issues_detected_by} />
          <Breakdown title="Tasks created by" data={analytics.ai_vs_human.tasks_created_by} />
          <Breakdown title="Actions by status" data={analytics.actions_summary} />
          <div className="rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
            <h2 className="text-sm font-semibold tracking-tight">Average resolution time</h2>
            <p className="mt-2 text-2xl font-semibold">
              {analytics.average_resolution_minutes !== null
                ? `${analytics.average_resolution_minutes.toFixed(1)} min`
                : "no resolved issues yet"}
            </p>
          </div>
        </div>
      ) : (
        <p className="text-sm text-zinc-500">Could not load analytics.</p>
      )}
    </main>
  );
}

function Breakdown({ title, data }: { title: string; data: Record<string, number> }) {
  const entries = Object.entries(data);
  return (
    <div className="rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
      <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
      <ul className="mt-2 space-y-1 text-sm">
        {entries.map(([key, value]) => (
          <li key={key} className="flex justify-between gap-2">
            <span className="text-zinc-500">{key}</span>
            <span className="font-medium">{value}</span>
          </li>
        ))}
        {entries.length === 0 ? <li className="text-zinc-500">No data yet.</li> : null}
      </ul>
    </div>
  );
}
