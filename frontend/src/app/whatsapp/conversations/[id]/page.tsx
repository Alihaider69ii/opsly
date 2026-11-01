import { Suspense } from "react";
import { notFound, redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { StatusBadge } from "@/components/status-badge";
import {
  getCurrentOrganization,
  getWhatsAppConsent,
  getWhatsAppConversation,
  getWhatsAppMessages,
  requireUser,
} from "@/lib/dal";
import { formatDateTime, isUuid } from "@/lib/format";
import { ConsentPanel } from "./consent-panel";
import { HandoffPanel } from "./handoff-panel";

export const metadata = { title: "Conversation · WhatsApp · BizFlow AI" };

export default function ConversationPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading conversation…</p>}>
      <Conversation params={params} />
    </Suspense>
  );
}

async function Conversation({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!isUuid(id)) notFound();
  await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");
  const canManage = org.role === "OWNER" || org.role === "ADMIN";

  const conversation = await getWhatsAppConversation(id);
  if (!conversation) notFound();
  const [messages, consent] = await Promise.all([
    getWhatsAppMessages(id),
    getWhatsAppConsent(conversation.contact_id),
  ]);

  const windowOpen =
    conversation.window_expires_at !== null &&
    new Date(conversation.window_expires_at) > new Date();

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 space-y-8 p-8">
      <AppHeader current="whatsapp" />
      <div>
        <p className="text-sm text-zinc-500">
          <a href="/whatsapp" className="hover:underline">
            WhatsApp
          </a>{" "}
          · Conversation
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">
          {conversation.message_count} message{conversation.message_count === 1 ? "" : "s"}
        </h1>
      </div>

      <section
        aria-label="Window"
        className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800"
      >
        <dl className="grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
          <div className="flex gap-1.5">
            <dt className="text-zinc-500">Last inbound:</dt>
            <dd className="font-medium">{formatDateTime(conversation.last_inbound_at)}</dd>
          </div>
          <div className="flex gap-1.5">
            <dt className="text-zinc-500">Last outbound:</dt>
            <dd className="font-medium">{formatDateTime(conversation.last_outbound_at)}</dd>
          </div>
        </dl>
        <span className="flex items-center gap-2 text-sm">
          <span className="text-zinc-500">Customer-service window:</span>
          <StatusBadge status={windowOpen ? "OPEN" : "CLOSED"} />
        </span>
      </section>

      <section
        aria-label="Human handoff"
        className="space-y-3 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800"
      >
        <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
          AI / human handoff
        </h2>
        <HandoffPanel conversationId={id} initial={conversation} canManage={canManage} />
      </section>

      <section aria-label="Messages" className="space-y-3">
        <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
          Message history
        </h2>
        <ul className="space-y-2">
          {(messages ?? []).map((message) => (
            <li
              key={message.id}
              data-message={message.id}
              data-direction={message.direction}
              className={`max-w-[85%] space-y-1 rounded-lg border p-3 text-sm ${
                message.direction === "OUTBOUND"
                  ? "ml-auto border-blue-200 bg-blue-50 dark:border-blue-900 dark:bg-blue-950"
                  : "border-zinc-200 dark:border-zinc-800"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-zinc-500">
                  {message.direction === "OUTBOUND" ? "Sent" : "Received"} ·{" "}
                  {formatDateTime(message.created_at)}
                </span>
                <StatusBadge status={message.status} />
              </div>
              <p>
                {message.content ??
                  (message.template_name
                    ? `Template: ${message.template_name} (${message.template_language})`
                    : "(no text content)")}
              </p>
              {message.error_code ? (
                <p className="text-xs text-red-700 dark:text-red-400">{message.error_code}</p>
              ) : null}
              {message.ai ? (
                <div data-testid="ai-metadata" className="flex flex-wrap items-center gap-1.5 pt-1">
                  <StatusBadge status={message.ai.grounded ? "GROUNDED" : "UNGROUNDED"} />
                  <StatusBadge status={message.ai.confidence} />
                  <span className="text-xs text-zinc-500">
                    {message.ai.sources_count} source{message.ai.sources_count === 1 ? "" : "s"}
                  </span>
                  {message.policy?.reason ? (
                    <span className="text-xs text-zinc-500">· {message.policy.reason}</span>
                  ) : null}
                </div>
              ) : null}
            </li>
          ))}
          {(messages ?? []).length === 0 ? (
            <li className="text-sm text-zinc-500">No messages yet.</li>
          ) : null}
        </ul>
      </section>

      <section aria-label="Preferences" className="space-y-3">
        <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
          Consent and suppression
        </h2>
        {consent ? (
          <ConsentPanel
            contactId={conversation.contact_id}
            initial={consent}
            canManage={canManage}
          />
        ) : (
          <p className="text-sm text-zinc-500">Consent information is unavailable.</p>
        )}
      </section>
    </main>
  );
}
