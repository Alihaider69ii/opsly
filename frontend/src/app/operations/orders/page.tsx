import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { PriorityBadge } from "@/components/priority-badge";
import { StatusBadge } from "@/components/status-badge";
import { getCurrentOrganization, getCurrentUser, getOrdersNeedingAttention } from "@/lib/dal";

export const metadata = { title: "Orders · Operations · BizFlow AI" };

export default function OrdersPage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading orders…</p>}>
      <Orders />
    </Suspense>
  );
}

async function Orders() {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");

  const rows = await getOrdersNeedingAttention();

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 space-y-8 p-8">
      <AppHeader current="operations" />
      <div>
        <p className="text-sm text-zinc-500">
          <Link href="/operations" className="hover:underline">
            Operations
          </Link>{" "}
          · Orders
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">Orders Needing Attention</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Every order with at least one open operational issue -- unfulfilled, high-value,
          duplicate, payment or warehouse signals.
        </p>
      </div>

      <ul className="space-y-2" data-testid="order-list">
        {rows.map((row) => (
          <li key={row.issue_id} data-order={row.order_id} className="rounded-lg border border-zinc-200 p-3 dark:border-zinc-800">
            <Link href={`/operations/issues/${row.issue_id}`} className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span className="min-w-0 flex-1">
                <span className="font-medium">{row.order_number ?? row.order_id}</span>{" "}
                <span className="text-zinc-500">{row.issue_title}</span>
              </span>
              <span className="flex shrink-0 items-center gap-1.5">
                <StatusBadge status={row.issue_category} />
                <PriorityBadge priority={row.issue_priority} />
                <span className="text-xs text-zinc-500">
                  {row.total ? `$${row.total}` : ""} {row.fulfillment_status ?? ""}
                </span>
              </span>
            </Link>
          </li>
        ))}
        {rows.length === 0 ? <li className="text-sm text-zinc-500">No orders currently need attention.</li> : null}
      </ul>
    </main>
  );
}
