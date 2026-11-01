import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { getCurrentOrganization, getCurrentUser, getPaymentOrders } from "@/lib/dal";

export const metadata = { title: "Payments · Operations · BizFlow AI" };

export default function PaymentsPage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading payments…</p>}>
      <Payments />
    </Suspense>
  );
}

async function Payments() {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");

  const rows = await getPaymentOrders();

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 space-y-8 p-8">
      <AppHeader current="operations" />
      <div>
        <p className="text-sm text-zinc-500">
          <Link href="/operations" className="hover:underline">
            Operations
          </Link>{" "}
          · Payments
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">Payment Issues</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Orders pending, failed, voided or refunded -- read directly from Shopify&apos;s own
          financial_status, never a duplicated payment record.
        </p>
      </div>

      <ul className="space-y-2" data-testid="payment-list">
        {rows.map((row) => (
          <li key={row.order_id} data-order={row.order_id} className="flex items-center justify-between gap-2 rounded-lg border border-zinc-200 p-3 text-sm dark:border-zinc-800">
            <span className="font-medium">{row.order_number ?? row.order_id}</span>
            <span className="text-zinc-500">
              {row.financial_status ?? "unknown"} {row.total ? `· $${row.total}` : ""}
            </span>
          </li>
        ))}
        {rows.length === 0 ? <li className="text-sm text-zinc-500">No payment issues right now.</li> : null}
      </ul>
    </main>
  );
}
