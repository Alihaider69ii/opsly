import { Suspense } from "react";
import { AdminHeader } from "@/components/admin-header";
import { getBrands, requirePlatformAdmin } from "@/lib/dal";
import { enterBrand, setBrandActive } from "../actions";
import { CreateBrandForm } from "./create-brand-form";

export const metadata = { title: "Brands · Platform Admin · BizFlow AI" };

export default function BrandsPage(props: PageProps<"/admin/brands">) {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading…</p>}>
      <Brands searchParams={props.searchParams} />
    </Suspense>
  );
}

async function Brands({ searchParams }: { searchParams: PageProps<"/admin/brands">["searchParams"] }) {
  await requirePlatformAdmin();
  const params = await searchParams;
  const environment = typeof params.environment === "string" ? params.environment : undefined;
  const search = typeof params.search === "string" ? params.search : undefined;
  const brands = await getBrands({ environment, search });

  return (
    <main className="mx-auto w-full max-w-5xl flex-1 space-y-8 p-8">
      <AdminHeader current="brands" />
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Brands</h1>
        <p className="mt-1 text-sm text-zinc-500">Every organization on BizFlow AI.</p>
      </div>

      <form method="get" className="flex flex-wrap items-end gap-3 text-sm">
        <label className="space-y-1">
          <span className="block text-zinc-500">Search</span>
          <input
            type="text"
            name="search"
            defaultValue={search}
            className="rounded-md border border-zinc-300 bg-transparent px-3 py-1.5 dark:border-zinc-700"
          />
        </label>
        <label className="space-y-1">
          <span className="block text-zinc-500">Environment</span>
          <select
            name="environment"
            defaultValue={environment ?? ""}
            className="rounded-md border border-zinc-300 bg-transparent px-3 py-1.5 dark:border-zinc-700"
          >
            <option value="">All</option>
            <option value="DEMO">Demo</option>
            <option value="PRODUCTION">Production</option>
          </select>
        </label>
        <button type="submit" className="rounded-md bg-zinc-900 px-3 py-1.5 text-white dark:bg-zinc-100 dark:text-zinc-900">
          Filter
        </button>
      </form>

      <ul className="space-y-2" data-testid="brand-list">
        {brands.map((brand) => (
          <li
            key={brand.id}
            data-brand={brand.slug}
            className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-zinc-200 p-4 text-sm dark:border-zinc-800"
          >
            <div>
              <p className="font-medium">
                {brand.name}{" "}
                <span className="text-zinc-500">
                  ({brand.environment}
                  {brand.is_active ? "" : " · INACTIVE"})
                </span>
              </p>
              <p className="text-zinc-500">
                Owner: {brand.owner_email ?? "—"} · {brand.user_count} users ·{" "}
                {brand.customer_count} customers · {brand.open_issues} open issues ·{" "}
                {brand.open_tasks} open tasks
              </p>
            </div>
            <div className="flex items-center gap-2">
              <form action={enterBrand}>
                <input type="hidden" name="organization_id" value={brand.id} />
                <button type="submit" className="rounded-md bg-zinc-900 px-3 py-1.5 text-white dark:bg-zinc-100 dark:text-zinc-900">
                  Open Workspace
                </button>
              </form>
              <form action={setBrandActive.bind(null, brand.id, !brand.is_active)}>
                <button type="submit" className="rounded-md border border-zinc-300 px-3 py-1.5 dark:border-zinc-700">
                  {brand.is_active ? "Deactivate" : "Activate"}
                </button>
              </form>
            </div>
          </li>
        ))}
        {brands.length === 0 ? <li className="text-sm text-zinc-500">No brands match.</li> : null}
      </ul>

      <div className="rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
        <h2 className="font-semibold">Create Brand</h2>
        <CreateBrandForm />
      </div>
    </main>
  );
}
