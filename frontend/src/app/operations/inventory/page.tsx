import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { StatusBadge } from "@/components/status-badge";
import { getCurrentOrganization, getCurrentUser, getInventoryOverview } from "@/lib/dal";

export const metadata = { title: "Inventory · Operations · BizFlow AI" };

export default function InventoryPage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading inventory…</p>}>
      <Inventory />
    </Suspense>
  );
}

async function Inventory() {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");

  const rows = await getInventoryOverview();

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 space-y-8 p-8">
      <AppHeader current="operations" />
      <div>
        <p className="text-sm text-zinc-500">
          <Link href="/operations" className="hover:underline">
            Operations
          </Link>{" "}
          · Inventory
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">Inventory</h1>
      </div>

      <div className="overflow-x-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
        <table className="w-full text-sm" data-testid="inventory-table">
          <thead className="bg-zinc-50 text-left text-xs uppercase text-zinc-500 dark:bg-zinc-900">
            <tr>
              <th className="px-3 py-2">Product</th>
              <th className="px-3 py-2">SKU</th>
              <th className="px-3 py-2">Quantity</th>
              <th className="px-3 py-2">Alert</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.variant_id} data-variant={row.variant_id} className="border-t border-zinc-200 dark:border-zinc-800">
                <td className="px-3 py-2">{row.title}</td>
                <td className="px-3 py-2 text-zinc-500">{row.sku ?? "—"}</td>
                <td className="px-3 py-2">{row.inventory_quantity ?? "unknown"}</td>
                <td className="px-3 py-2">
                  {row.out_of_stock ? (
                    <StatusBadge status="FAILED" />
                  ) : row.low_stock ? (
                    <StatusBadge status="WAITING" />
                  ) : (
                    <StatusBadge status="READY" />
                  )}
                </td>
              </tr>
            ))}
            {rows.length === 0 ? (
              <tr>
                <td colSpan={4} className="px-3 py-4 text-center text-zinc-500">
                  No product variants synced yet.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </main>
  );
}
