import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { StatusBadge } from "@/components/status-badge";
import {
  getCurrentOrganization,
  getWhatsAppAutomationUsage,
  getWhatsAppConversations,
  getWhatsAppStatus,
  getWhatsAppTemplates,
  requireUser,
} from "@/lib/dal";
import { formatDateTime } from "@/lib/format";
import { AISettingsForm } from "./ai-settings-form";
import { ConnectForm } from "./connect-form";
import { ConnectionActions } from "./connection-actions";

export const metadata = { title: "WhatsApp · BizFlow AI" };

type SearchParams = Promise<{ connected?: string; disconnected?: string }>;

export default function WhatsAppPage({ searchParams }: { searchParams: SearchParams }) {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading WhatsApp…</p>}>
      <WhatsApp searchParams={searchParams} />
    </Suspense>
  );
}

async function WhatsApp({ searchParams }: { searchParams: SearchParams }) {
  const { connected, disconnected } = await searchParams;
  await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");
  const status = await getWhatsAppStatus();
  if (!status) redirect("/login");
  const canManage = org.role === "OWNER" || org.role === "ADMIN";
  const connection = status.connection;
  const live = connection !== null && connection.status !== "DISCONNECTED";
  const ready = status.available && status.secure_storage;

  const [conversations, templates, automationUsage] = live
    ? await Promise.all([
        getWhatsAppConversations(),
        getWhatsAppTemplates(),
        getWhatsAppAutomationUsage(),
      ])
    : [[], [], []];

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 space-y-8 p-8">
      <AppHeader current="whatsapp" />
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">WhatsApp</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Connect a WhatsApp Business phone number so automations can message customers. Sending is
          always gated by consent, suppression and the customer-service window.
        </p>
      </div>

      {connected ? (
        <p
          role="status"
          className="rounded-md border border-green-300 bg-green-50 px-3 py-2 text-sm text-green-800 dark:border-green-900 dark:bg-green-950 dark:text-green-300"
        >
          Your WhatsApp number is connected.
        </p>
      ) : null}
      {disconnected ? (
        <p
          role="status"
          className="rounded-md border border-zinc-300 bg-zinc-50 px-3 py-2 text-sm dark:border-zinc-700 dark:bg-zinc-900"
        >
          Your WhatsApp number was disconnected. Conversation and consent history were kept.
        </p>
      ) : null}

      {!status.available ? (
        <p
          role="alert"
          data-testid="whatsapp-unavailable"
          className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200"
        >
          WhatsApp is not set up on this server yet. An administrator needs to add the Meta App
          credentials to the server configuration.
        </p>
      ) : !status.secure_storage ? (
        <p
          role="alert"
          data-testid="whatsapp-no-secure-storage"
          className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200"
        >
          Secure credential storage is not configured on this server, so a number cannot be
          connected yet. An administrator needs to set the encryption key.
        </p>
      ) : null}

      {live && connection ? (
        <>
          <section
            aria-label="Connection"
            data-testid="whatsapp-connection"
            className="space-y-4 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800"
          >
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <p className="font-medium">
                  {connection.verified_name ?? connection.display_phone_number ?? "WhatsApp number"}
                </p>
                <p data-field="phone-number-id" className="text-sm text-zinc-500">
                  {connection.phone_number_id}
                </p>
              </div>
              <StatusBadge status={connection.status} />
            </div>
            <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
              <Fact
                label="Connected"
                value={formatDateTime(connection.connected_at)}
                field="connected"
              />
              <Fact
                label="Last verified"
                value={formatDateTime(connection.last_verified_at)}
                field="last-verified"
              />
              <Fact label="Graph API version" value={connection.api_version} />
              <Fact
                label="Default country"
                value={connection.default_country ?? "not set"}
                field="default-country"
              />
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
              <ConnectionActions
                aiAutoReplyEnabled={connection.ai_auto_reply_enabled}
                canSyncTemplates={ready}
              />
            ) : (
              <p className="text-sm text-zinc-500">
                Only owners and admins can manage this connection.
              </p>
            )}
          </section>

          {canManage ? (
            <section
              aria-label="AI settings"
              data-testid="whatsapp-ai-settings-section"
              className="space-y-3 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800"
            >
              <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
                AI conversation settings
              </h2>
              <AISettingsForm connection={connection} />
            </section>
          ) : null}

          <section aria-label="Conversations" className="space-y-3">
            <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
              Conversations
            </h2>
            <ul className="space-y-2">
              {conversations.map((conversation) => (
                <li
                  key={conversation.id}
                  data-conversation={conversation.id}
                  data-ai-state={conversation.ai_state}
                  className="rounded-lg border border-zinc-200 p-3 dark:border-zinc-800"
                >
                  <Link
                    href={`/whatsapp/conversations/${conversation.id}`}
                    className="flex items-center justify-between gap-2 text-sm hover:underline"
                  >
                    <span>
                      <span className="font-medium">{conversation.message_count} messages</span>
                      <span className="text-zinc-500">
                        {" "}
                        · last inbound {formatDateTime(conversation.last_inbound_at)}
                      </span>
                    </span>
                    <span className="flex items-center gap-2">
                      {conversation.ai_state !== "AI_ACTIVE" ? (
                        <StatusBadge status={conversation.ai_state} />
                      ) : null}
                      <StatusBadge
                        status={
                          conversation.window_expires_at &&
                          new Date(conversation.window_expires_at) > new Date()
                            ? "OPEN"
                            : "CLOSED"
                        }
                      />
                    </span>
                  </Link>
                </li>
              ))}
              {conversations.length === 0 ? (
                <li className="text-sm text-zinc-500">No conversations yet.</li>
              ) : null}
            </ul>
          </section>

          <section aria-label="Templates" className="space-y-3">
            <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
              Message templates
            </h2>
            <ul className="space-y-2">
              {templates.map((template) => (
                <li
                  key={template.id}
                  data-template={template.name}
                  className="flex items-center justify-between gap-2 rounded-lg border border-zinc-200 p-3 text-sm dark:border-zinc-800"
                >
                  <span>
                    <span className="font-medium">{template.name}</span>
                    <span className="text-zinc-500"> ({template.language})</span>
                  </span>
                  <StatusBadge status={template.status} />
                </li>
              ))}
              {templates.length === 0 ? (
                <li className="text-sm text-zinc-500">
                  No templates synced yet. Approved templates are required to message a contact
                  outside the 24-hour customer-service window.
                </li>
              ) : null}
            </ul>
          </section>

          <section aria-label="Automation usage" className="space-y-3">
            <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
              Workflows using WhatsApp
            </h2>
            <ul className="space-y-2">
              {automationUsage.map((workflow) => (
                <li
                  key={workflow.workflow_id}
                  className="flex items-center justify-between gap-2 rounded-lg border border-zinc-200 p-3 text-sm dark:border-zinc-800"
                >
                  <Link href={`/automations/${workflow.workflow_id}`} className="hover:underline">
                    {workflow.name}
                  </Link>
                  <span className="flex items-center gap-2">
                    <span className="text-zinc-500">{workflow.whatsapp_actions.join(", ")}</span>
                    <StatusBadge status={workflow.status} />
                  </span>
                </li>
              ))}
              {automationUsage.length === 0 ? (
                <li className="text-sm text-zinc-500">
                  No automation workflow uses a WhatsApp action yet.
                </li>
              ) : null}
            </ul>
          </section>
        </>
      ) : ready ? (
        <section aria-label="Connect" className="space-y-3">
          <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
            Connect a WhatsApp number
          </h2>
          {canManage ? (
            <ConnectForm />
          ) : (
            <p className="text-sm text-zinc-500">
              Only owners and admins can connect a WhatsApp number.
            </p>
          )}
        </section>
      ) : null}
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
