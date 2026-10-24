import { Suspense } from "react";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { businessTypeLabel } from "@/lib/business-types";
import { getCurrentOrganization, getCurrentUser } from "@/lib/dal";

export const metadata = { title: "Dashboard · BizFlow AI" };

export default function DashboardPage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading your dashboard…</p>}>
      <Dashboard />
    </Suspense>
  );
}

async function Dashboard() {
  const [user, org] = await Promise.all([getCurrentUser(), getCurrentOrganization()]);
  if (!user) redirect("/login");
  if (!org) redirect("/onboarding");

  const details: [string, string | null][] = [
    ["Business", org.name],
    ["Website", org.website],
    ["Business type", businessTypeLabel(org.business_type)],
    ["Your role", org.role],
    ["Organization slug", org.slug],
  ];

  return (
    <main className="mx-auto w-full max-w-2xl flex-1 space-y-8 p-8">
      <AppHeader current="dashboard" />

      <h1 className="text-2xl font-semibold tracking-tight">Welcome back, {user.email}</h1>

      <dl className="divide-y divide-zinc-200 rounded-lg border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
        {details.map(([label, value]) => (
          <div key={label} className="flex justify-between gap-4 px-4 py-3 text-sm">
            <dt className="text-zinc-500">{label}</dt>
            <dd className="text-right font-medium">
              {label === "Website" && value ? (
                <a href={value} target="_blank" rel="noopener noreferrer" className="underline">
                  {value}
                </a>
              ) : (
                (value ?? "—")
              )}
            </dd>
          </div>
        ))}
      </dl>
    </main>
  );
}
