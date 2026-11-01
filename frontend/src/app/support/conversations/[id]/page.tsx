import { Suspense } from "react";
import { notFound, redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import {
  getCurrentOrganization,
  getSupportConversation,
  getSupportConversationTimeline,
  getSupportMessages,
  requireUser,
} from "@/lib/dal";
import { formatDateTime, isUuid } from "@/lib/format";
import { SupportPanel } from "./support-panel";

export const metadata = { title: "Conversation · Support · BizFlow AI" };

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

  const conversation = await getSupportConversation(id);
  if (!conversation) notFound();
  const [messages, timeline] = await Promise.all([
    getSupportMessages(id),
    getSupportConversationTimeline(id),
  ]);

  const customer = conversation.customer;

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 space-y-8 p-8">
      <AppHeader current="support" />
      <div>
        <p className="text-sm text-zinc-500">
          <a href="/support" className="hover:underline">
            Support
          </a>{" "}
          · Conversation
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">
          {customer?.first_name || customer?.email || customer?.phone || "Unknown customer"}
        </h1>
      </div>

      {customer ? (
        <section
          aria-label="Customer"
          data-testid="customer-panel"
          className="grid gap-x-6 gap-y-1 rounded-lg border border-zinc-200 p-4 text-sm sm:grid-cols-2 dark:border-zinc-800"
        >
          <Fact label="Email" value={customer.email ?? "not on file"} />
          <Fact label="Phone" value={customer.phone ?? "not on file"} />
          <Fact label="Marketing consent" value={customer.email_marketing_consent === true ? "opted in" : customer.email_marketing_consent === false ? "opted out" : "unknown"} />
          <Fact label="Email suppressed" value={customer.email_suppressed ? "yes" : "no"} />
        </section>
      ) : (
        <p className="text-sm text-zinc-500">No customer identity linked to this conversation yet.</p>
      )}

      <section aria-label="Conversation">
        {/* Remounts (resetting local state) whenever the server's view of this conversation
            actually changes -- e.g. an async worker action finishes after this page already
            rendered -- without the anti-pattern of syncing props into state via useEffect. */}
        <SupportPanel
          key={`${conversation.id}-${conversation.updated_at}`}
          conversationId={id}
          initial={conversation}
          initialMessages={messages ?? []}
        />
      </section>

      <section aria-label="Timeline" className="space-y-2">
        <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">Timeline</h2>
        <ul data-testid="timeline" className="space-y-1 text-sm">
          {(timeline ?? []).map((event) => (
            <li key={event.id} data-event={event.event_type} className="flex justify-between gap-2 text-zinc-600 dark:text-zinc-400">
              <span>{event.event_type.replace(/_/g, " ")}</span>
              <span className="text-xs text-zinc-500">{formatDateTime(event.created_at)}</span>
            </li>
          ))}
          {(timeline ?? []).length === 0 ? <li className="text-zinc-500">No activity recorded yet.</li> : null}
        </ul>
      </section>
    </main>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-1.5">
      <dt className="text-zinc-500">{label}:</dt>
      <dd className="font-medium">{value}</dd>
    </div>
  );
}
