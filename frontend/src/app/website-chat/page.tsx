import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { getChatWidget, getCurrentOrganization, requireUser } from "@/lib/dal";
import { SettingsForm } from "./settings-form";
import { WidgetPreview } from "./widget-preview";

export const metadata = { title: "Website Chat · BizFlow AI" };

// Public values: the widget runs in visitors' browsers and talks to the API directly.
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const APP_URL = process.env.NEXT_PUBLIC_APP_URL ?? "http://localhost:3000";

export default function WebsiteChatPage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading…</p>}>
      <WebsiteChat />
    </Suspense>
  );
}

async function WebsiteChat() {
  await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");
  const widget = await getChatWidget();
  if (!widget) redirect("/login");
  const canManage = org.role === "OWNER" || org.role === "ADMIN";

  const snippet = `<script src="${APP_URL}/widget.js" data-key="${widget.public_key}" data-api="${API_URL}" async></script>`;

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 space-y-8 p-8">
      <AppHeader current="website-chat" />
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Website Chat</h1>
        <p className="mt-1 text-sm text-zinc-500">
          An assistant on your website that answers customer questions from your{" "}
          <Link href="/knowledge" className="underline">
            Knowledge Base
          </Link>
          . Try it: the chat button is at the bottom right of this page.
        </p>
      </div>

      <section aria-label="Install" className="space-y-3">
        <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">Install</h2>
        <p className="text-sm">
          Paste this just before the closing <code>&lt;/body&gt;</code> tag of your website.
        </p>
        <pre
          data-testid="embed-snippet"
          className="overflow-x-auto whitespace-pre-wrap break-all rounded-lg border border-zinc-200 bg-zinc-50 p-3 font-mono text-xs dark:border-zinc-800 dark:bg-zinc-900"
        >
          {snippet}
        </pre>
        <p className="text-sm text-zinc-500">
          The key is public. It only tells the widget which business it belongs to, and cannot be
          used to read your data.
        </p>
      </section>

      {canManage ? (
        <section aria-label="Settings" className="space-y-3">
          <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">Settings</h2>
          <SettingsForm widget={widget} />
        </section>
      ) : (
        <p className="text-sm text-zinc-500">Only owners and admins can change these settings.</p>
      )}

      {widget.enabled ? (
        <WidgetPreview publicKey={widget.public_key} apiUrl={API_URL} />
      ) : (
        <p className="text-sm text-amber-700 dark:text-amber-400">
          The widget is disabled, so visitors will not see it.
        </p>
      )}
    </main>
  );
}
