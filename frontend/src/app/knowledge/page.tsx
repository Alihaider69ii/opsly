import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { StatusBadge } from "@/components/status-badge";
import { getCurrentOrganization, getKnowledgeSources, requireUser } from "@/lib/dal";
import { crawlSummary, formatDateTime, hostOf, isInProgress } from "@/lib/format";
import { AddWebsiteForm } from "./add-website-form";
import { AutoRefresh } from "./auto-refresh";
import { SourceActions } from "./source-actions";

export const metadata = { title: "Knowledge Base · BizFlow AI" };

export default function KnowledgePage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading your knowledge base…</p>}>
      <Knowledge />
    </Suspense>
  );
}

async function Knowledge() {
  await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");
  const sources = await getKnowledgeSources();
  const canManage = org.role === "OWNER" || org.role === "ADMIN";

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 space-y-8 p-8">
      <AppHeader current="knowledge" />
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Knowledge Base</h1>
        <p className="mt-1 text-sm text-zinc-500">
          What your AI assistant will know about {org.name}. Add your website and we will read it.
        </p>
      </div>

      {canManage ? (
        <AddWebsiteForm />
      ) : (
        <p className="text-sm text-zinc-500">
          Only owners and admins can add or change knowledge sources.
        </p>
      )}

      <section aria-label="Sources" className="space-y-4">
        <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">Sources</h2>
        {sources.length === 0 ? (
          <p className="rounded-lg border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500 dark:border-zinc-700">
            No sources yet. Add your website to get started.
          </p>
        ) : (
          <ul className="space-y-4">
            {sources.map((source) => (
              <li
                key={source.id}
                data-source-id={source.id}
                className="space-y-3 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800"
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <Link
                      href={`/knowledge/${source.id}`}
                      className="font-medium underline-offset-4 hover:underline"
                    >
                      {hostOf(source.url) || source.name}
                    </Link>
                    <p className="text-sm text-zinc-500">{source.url}</p>
                  </div>
                  <StatusBadge status={source.status} />
                </div>
                <dl className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
                  <div className="flex gap-1.5">
                    <dt className="text-zinc-500">Pages:</dt>
                    <dd data-field="pages" className="font-medium">
                      {source.document_count}
                    </dd>
                  </div>
                  <div className="flex gap-1.5">
                    <dt className="text-zinc-500">Last crawled:</dt>
                    <dd className="font-medium">{formatDateTime(source.last_crawled_at)}</dd>
                  </div>
                </dl>
                {source.status === "FAILED" && source.error_message ? (
                  <p role="alert" className="text-sm text-red-700 dark:text-red-400">
                    {source.error_message}
                  </p>
                ) : null}
                {source.status === "COMPLETED" ? (
                  <p className="text-sm text-zinc-500">{crawlSummary(source.crawl_stats)}</p>
                ) : null}
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <Link href={`/knowledge/${source.id}`} className="text-sm underline">
                    Open
                  </Link>
                  {canManage ? <SourceActions sourceId={source.id} status={source.status} /> : null}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
      <AutoRefresh active={sources.some((s) => isInProgress(s.status))} />
    </main>
  );
}
