import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { StatusBadge } from "@/components/status-badge";
import { getCurrentOrganization, getCurrentUser, getShipments } from "@/lib/dal";
import { formatDateTime } from "@/lib/format";

export const metadata = { title: "Shipping · Operations · BizFlow AI" };

type SearchParams = Promise<{ status?: string }>;
const STATUSES = ["All", "PENDING", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED", "DELAYED", "NDR", "RTO", "LOST"];

export default function ShippingPage({ searchParams }: { searchParams: SearchParams }) {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading shipments…</p>}>
      <Shipping searchParams={searchParams} />
    </Suspense>
  );
}

async function Shipping({ searchParams }: { searchParams: SearchParams }) {
  const { status } = await searchParams;
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");

  const params = new URLSearchParams();
  if (status && status !== "All") params.set("status", status);
  const query = params.toString() ? `?${params.toString()}` : "";
  const shipments = await getShipments(query);

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 space-y-8 p-8">
      <AppHeader current="operations" />
      <div>
        <p className="text-sm text-zinc-500">
          <Link href="/operations" className="hover:underline">
            Operations
          </Link>{" "}
          · Shipping
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">Shipping / NDR / RTO</h1>
      </div>

      <div className="flex flex-wrap gap-1 text-xs" role="group" aria-label="status">
        {STATUSES.map((option) => {
          const href = option === "All" ? "/operations/shipping" : `/operations/shipping?status=${option}`;
          const active = option === (status ?? "All");
          return (
            <Link
              key={option}
              href={href}
              data-filter={`status-${option}`}
              className={`rounded-md px-2 py-1 ${
                active ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900" : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-900"
              }`}
            >
              {option}
            </Link>
          );
        })}
      </div>

      <ul className="space-y-2" data-testid="shipment-list">
        {shipments.map((shipment) => (
          <li key={shipment.id} data-shipment={shipment.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-zinc-200 p-3 text-sm dark:border-zinc-800">
            <span>
              Order {shipment.order_id.slice(0, 8)} · {shipment.carrier ?? "unknown carrier"} ·{" "}
              {shipment.tracking_number ?? "no tracking number"}
            </span>
            <span className="flex items-center gap-2">
              <StatusBadge status={shipment.status} />
              {shipment.ndr_count > 0 ? <span className="text-xs text-zinc-500">NDR x{shipment.ndr_count}</span> : null}
              <span className="text-xs text-zinc-500">
                {shipment.estimated_delivery_at ? `ETA ${formatDateTime(shipment.estimated_delivery_at)}` : ""}
              </span>
            </span>
          </li>
        ))}
        {shipments.length === 0 ? <li className="text-sm text-zinc-500">No shipments match this filter.</li> : null}
      </ul>
    </main>
  );
}
