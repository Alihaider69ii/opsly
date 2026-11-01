import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { StatusBadge } from "@/components/status-badge";
import { getCurrentOrganization, getEmailAutomationUsage, getEmailStatus, requireUser } from "@/lib/dal";
import { formatDateTime } from "@/lib/format";
import { AISettingsForm } from "./ai-settings-form";
import { ConnectForm } from "./connect-form";
import { ConnectionActions } from "./connection-actions";

export const metadata = { title: "Email · BizFlow AI" };

type SearchParams = Promise<{ connected?: string; disconnected?: string }>;

export default function EmailPage({ searchParams }: { searchParams: SearchParams }) {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading email…</p>}>
      <Email searchParams={searchParams} />
    </Suspense>
  );
}

async function Email({ searchParams }: { searchParams: SearchParams }) {
  const { connected, disconnected } = await searchParams;
  await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");
  const status = await getEmailStatus();
  if (!status) redirect("/login");
  const canManage = org.role === "OWNER" || org.role === "ADMIN";
  const connection = status.connection;
  const live = connection !== null && connection.status !== "DISCONNECTED";

  const automationUsage = live ? await getEmailAutomationUsage() : [];

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 space-y-8 p-8">
      <AppHeader current="email" />
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Email</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Customer support, lifecycle and marketing email, all through one connection. Marketing
          email always respects consent and suppression.
        </p>
        <nav className="mt-3 flex gap-4 text-sm">
          <Link href="/email/templates" className="hover:underline">
            Templates
          </Link>
          <Link href="/email/campaigns" className="hover:underline">
            Campaigns
          </Link>
        </nav>
      </div>

      {connected ? (
        <p role="status" className="rounded-md border border-green-300 bg-green-50 px-3 py-2 text-sm text-green-800 dark:border-green-900 dark:bg-green-950 dark:text-green-300">
          Email is connected.
        </p>
      ) : null}
      {disconnected ? (
        <p role="status" className="rounded-md border border-zinc-300 bg-zinc-50 px-3 py-2 text-sm dark:border-zinc-700 dark:bg-zinc-900">
          Email was disconnected. Templates and campaigns were kept.
        </p>
      ) : null}

      {!status.available ? (
        <p role="alert" data-testid="email-unavailable" className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          Email is not set up on this server yet. An administrator needs to add EMAIL_API_KEY to
          the server configuration.
        </p>
      ) : null}

      {live && connection ? (
        <>
          <section aria-label="Connection" data-testid="email-connection" className="space-y-4 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <p className="font-medium">{connection.from_name ?? connection.from_address}</p>
                <p data-field="from-address" className="text-sm text-zinc-500">
                  {connection.from_address}
                </p>
              </div>
              <StatusBadge status={connection.status} />
            </div>
            <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
              <Fact label="Connected" value={formatDateTime(connection.connected_at)} />
              <Fact label="Inbound address" value={connection.inbound_address} field="inbound-address" />
              <Fact label="Reply-to" value={connection.reply_to_address ?? "not set"} />
              <Fact
                label="AI auto-reply"
                value={connection.ai_auto_reply_enabled ? "enabled" : "disabled (default)"}
                field="ai-auto-reply"
              />
            </dl>
            {connection.status === "ERROR" && connection.error_message ? (
              <p role="alert" className="text-sm text-red-700 dark:text-red-400">
                {connection.error_message}
              </p>
            ) : null}
            {canManage ? (
              <ConnectionActions aiAutoReplyEnabled={connection.ai_auto_reply_enabled} />
            ) : (
              <p className="text-sm text-zinc-500">Only owners and admins can manage this connection.</p>
            )}
          </section>

          {canManage ? (
            <section aria-label="AI settings" data-testid="email-ai-settings-section" className="space-y-3 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
              <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">AI conversation settings</h2>
              <AISettingsForm connection={connection} />
            </section>
          ) : null}

          <section aria-label="Automation usage" className="space-y-3">
            <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">Workflows using email</h2>
            <ul className="space-y-2">
              {automationUsage.map((workflow) => (
                <li key={workflow.workflow_id} className="flex items-center justify-between gap-2 rounded-lg border border-zinc-200 p-3 text-sm dark:border-zinc-800">
                  <Link href={`/automations/${workflow.workflow_id}`} className="hover:underline">
                    {workflow.name}
                  </Link>
                  <span className="flex items-center gap-2">
                    <span className="text-zinc-500">{workflow.email_actions.join(", ")}</span>
                    <StatusBadge status={workflow.status} />
                  </span>
                </li>
              ))}
              {automationUsage.length === 0 ? (
                <li className="text-sm text-zinc-500">No automation workflow uses an email action yet.</li>
              ) : null}
            </ul>
          </section>
        </>
      ) : (
        <section aria-label="Connect" className="space-y-3">
          <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">Connect email</h2>
          {canManage ? (
            <ConnectForm />
          ) : (
            <p className="text-sm text-zinc-500">Only owners and admins can connect email.</p>
          )}
        </section>
      )}
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
