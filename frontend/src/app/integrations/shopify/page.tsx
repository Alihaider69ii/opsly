import { Suspense } from "react";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { StatusBadge } from "@/components/status-badge";
import { getCurrentOrganization, getShopifyStatus, requireUser } from "@/lib/dal";
import { formatDateTime } from "@/lib/format";
import { RESOURCE_LABELS, isActiveSync, noticeFor, scopeList, webhookLabel } from "@/lib/shopify";
import { AutoRefresh } from "@/app/knowledge/auto-refresh";
import { ConnectForm } from "./connect-form";
import { StoreActions } from "./store-actions";

export const metadata = { title: "Shopify · BizFlow AI" };

type SearchParams = Promise<{ connected?: string; disconnected?: string; error?: string }>;

export default function ShopifyPage({ searchParams }: { searchParams: SearchParams }) {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading your Shopify store…</p>}>
      <Shopify searchParams={searchParams} />
    </Suspense>
  );
}

async function Shopify({ searchParams }: { searchParams: SearchParams }) {
  const { connected, disconnected, error } = await searchParams;
  await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");
  const status = await getShopifyStatus();
  if (!status) redirect("/login");
  const canManage = org.role === "OWNER" || org.role === "ADMIN";
  const connection = status.connection;
  const live = connection !== null && connection.status !== "DISCONNECTED";
  const syncing = isActiveSync(connection?.status);
  const ready = status.available && status.secure_storage;

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 space-y-8 p-8">
      <AppHeader current="shopify" />
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Shopify</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Connect your store so BizFlow can learn your products, customers and orders. BizFlow only
          reads from Shopify; it never changes your store.
        </p>
      </div>

      {connected ? (
        <p
          role="status"
          className="rounded-md border border-green-300 bg-green-50 px-3 py-2 text-sm text-green-800 dark:border-green-900 dark:bg-green-950 dark:text-green-300"
        >
          Your Shopify store is connected. The first import has started.
        </p>
      ) : null}
      {disconnected ? (
        <p
          role="status"
          className="rounded-md border border-zinc-300 bg-zinc-50 px-3 py-2 text-sm dark:border-zinc-700 dark:bg-zinc-900"
        >
          Your Shopify store was disconnected and its imported data was removed.
        </p>
      ) : null}
      {error ? (
        <p
          role="alert"
          className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300"
        >
          {noticeFor(error)}
        </p>
      ) : null}

      {!status.available ? (
        <p
          role="alert"
          data-testid="shopify-unavailable"
          className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200"
        >
          Shopify is not set up on this server yet. An administrator needs to add the Shopify app
          credentials to the server configuration.
        </p>
      ) : !status.secure_storage ? (
        <p
          role="alert"
          data-testid="shopify-no-secure-storage"
          className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200"
        >
          Secure credential storage is not configured on this server, so a store cannot be connected
          yet. An administrator needs to set the encryption key.
        </p>
      ) : null}

      {live && connection ? (
        <>
          <section
            aria-label="Store"
            data-testid="shopify-store"
            className="space-y-4 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800"
          >
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <p className="font-medium">{connection.shop_name ?? connection.shop_domain}</p>
                <p data-field="domain" className="text-sm text-zinc-500">
                  {connection.shop_domain}
                </p>
              </div>
              <StatusBadge status={connection.status} />
            </div>
            <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
              <Fact
                label="Connected"
                value={formatDateTime(connection.installed_at)}
                field="installed"
              />
              <Fact
                label="Last sync"
                value={formatDateTime(connection.last_sync_at)}
                field="last-sync"
              />
              <Fact label="Currency" value={connection.currency ?? "unknown"} />
              <Fact label="Time zone" value={connection.timezone ?? "unknown"} />
              <Fact label="Shopify API version" value={connection.api_version} />
              <Fact label="Live updates" value={webhookLabel(connection.webhook_status)} />
            </dl>
            {connection.status === "ERROR" && connection.error_message ? (
              <p role="alert" className="text-sm text-red-700 dark:text-red-400">
                {connection.error_message}
              </p>
            ) : null}
            {canManage ? (
              <StoreActions syncing={syncing} needsReconnect={connection.status === "ERROR"} />
            ) : (
              <p className="text-sm text-zinc-500">
                Only owners and admins can sync or disconnect the store.
              </p>
            )}
          </section>

          {connection.status === "ERROR" && canManage && ready ? (
            <section aria-label="Reconnect" className="space-y-3">
              <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
                Reconnect
              </h2>
              <p className="text-sm text-zinc-500">
                Shopify no longer accepts the saved access. Approve the app again to resume syncing.
              </p>
              <ConnectForm defaultShop={connection.shop_domain} submitLabel="Reconnect Shopify" />
            </section>
          ) : null}

          <section aria-label="Imported data" className="space-y-3">
            <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
              Imported data
            </h2>
            <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {(
                [
                  ["products", "Products"],
                  ["variants", "Variants"],
                  ["customers", "Customers"],
                  ["orders", "Orders"],
                ] as const
              ).map(([key, label]) => (
                <div
                  key={key}
                  className="rounded-lg border border-zinc-200 p-3 dark:border-zinc-800"
                >
                  <dt className="text-xs text-zinc-500">{label}</dt>
                  <dd data-count={key} className="text-xl font-semibold">
                    {status.counts[key]}
                  </dd>
                </div>
              ))}
            </dl>
          </section>

          <section aria-label="Sync progress" className="space-y-3">
            <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
              Sync progress
            </h2>
            <ul className="space-y-2">
              {status.sync.map((stage) => (
                <li
                  key={stage.resource}
                  data-stage={stage.resource}
                  className="space-y-1 rounded-lg border border-zinc-200 p-3 dark:border-zinc-800"
                >
                  <div className="flex items-center justify-between gap-2 text-sm">
                    <span className="font-medium">
                      {RESOURCE_LABELS[stage.resource] ?? stage.resource}
                    </span>
                    <span className="flex items-center gap-3">
                      <span data-field="synced" className="text-zinc-500">
                        {stage.items_synced} synced
                      </span>
                      <StatusBadge status={stage.status} />
                    </span>
                  </div>
                  {stage.error_message ? (
                    <p
                      className={`text-sm ${
                        stage.status === "FAILED"
                          ? "text-red-700 dark:text-red-400"
                          : "text-amber-700 dark:text-amber-400"
                      }`}
                    >
                      {stage.error_message}
                    </p>
                  ) : null}
                </li>
              ))}
              {status.sync.length === 0 ? (
                <li className="text-sm text-zinc-500">No sync has run yet.</li>
              ) : null}
            </ul>
          </section>

          <section aria-label="Permissions" className="space-y-1">
            <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
              Permissions granted
            </h2>
            <p
              data-testid="shopify-scopes"
              className="font-mono text-xs text-zinc-600 dark:text-zinc-400"
            >
              {scopeList(status.scopes)}
            </p>
          </section>
        </>
      ) : ready ? (
        <section aria-label="Connect" className="space-y-3">
          <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
            Connect your store
          </h2>
          {canManage ? (
            <>
              <ConnectForm />
              <p className="text-sm text-zinc-500">
                You will be taken to Shopify to approve read-only access to your products, customers
                and orders.
              </p>
            </>
          ) : (
            <p className="text-sm text-zinc-500">
              Only owners and admins can connect a Shopify store.
            </p>
          )}
        </section>
      ) : null}
      <AutoRefresh active={syncing} />
    </main>
  );
}

function Fact({ label, value, field }: { label: string; value: string; field?: string }) {
  return (
    <div className="flex gap-1.5">
      <dt className="text-zinc-500">{label}:</dt>
      <dd data-field={field} className="font-medium">
        {value}
      </dd>
    </div>
  );
}
