import { Suspense } from "react";
import { AdminHeader } from "@/components/admin-header";
import { getPlatformDashboard, requirePlatformAdmin } from "@/lib/dal";

export const metadata = { title: "Platform Admin · BizFlow AI" };

export default function AdminDashboardPage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading…</p>}>
      <Dashboard />
    </Suspense>
  );
}

const CARDS: { key: keyof NonNullable<Awaited<ReturnType<typeof getPlatformDashboard>>>; label: string }[] = [
  { key: "total_brands", label: "Total brands" },
  { key: "active_brands", label: "Active brands" },
  { key: "demo_brands", label: "Demo brands" },
  { key: "production_brands", label: "Production brands" },
  { key: "total_users", label: "Total users" },
  { key: "total_customers", label: "Total customers" },
  { key: "total_conversations", label: "Total conversations" },
  { key: "open_support_issues", label: "Open support issues" },
  { key: "open_operational_issues", label: "Open operational issues" },
  { key: "open_operational_tasks", label: "Open tasks" },
  { key: "ai_queries_recent", label: "AI queries answered" },
  { key: "connected_integrations", label: "Connected integrations" },
];

async function Dashboard() {
  await requirePlatformAdmin();
  const kpis = await getPlatformDashboard();

  return (
    <main className="mx-auto w-full max-w-5xl flex-1 space-y-8 p-8">
      <AdminHeader current="overview" />
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Platform Overview</h1>
        <p className="mt-1 text-sm text-zinc-500">Real-time counts across every brand.</p>
      </div>
      {kpis ? (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          {CARDS.map((card) => (
            <div
              key={card.key}
              className="rounded-lg border border-zinc-200 p-4 dark:border-zinc-800"
            >
              <p className="text-sm text-zinc-500">{card.label}</p>
              <p className="mt-1 text-2xl font-semibold tabular-nums">{kpis[card.key]}</p>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-sm text-zinc-500">Platform metrics are not available right now.</p>
      )}
    </main>
  );
}
