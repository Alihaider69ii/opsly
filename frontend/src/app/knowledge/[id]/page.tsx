import { Suspense } from "react";
import Link from "next/link";
import { notFound, redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { StatusBadge } from "@/components/status-badge";
import {
  getCurrentOrganization,
  getKnowledgeSource,
  getSourceDocuments,
  requireUser,
} from "@/lib/dal";
import { crawlSummary, formatDateTime, isInProgress, isUuid } from "@/lib/format";
import { AutoRefresh } from "../auto-refresh";
import { SourceActions } from "../source-actions";

export const metadata = { title: "Knowledge source · BizFlow AI" };

const PAGE_SIZE = 25;

type Props = {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ page?: string }>;
};

export default function SourcePage({ params, searchParams }: Props) {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading…</p>}>
      <Source params={params} searchParams={searchParams} />
    </Suspense>
  );
}

async function Source({ params, searchParams }: Props) {
  const { id } = await params;
  const { page: pageParam } = await searchParams;
  if (!isUuid(id)) notFound();
  await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");

  const page = Math.max(1, Number.parseInt(pageParam ?? "1", 10) || 1);
  const [source, documents] = await Promise.all([
    getKnowledgeSource(id),
    getSourceDocuments(id, PAGE_SIZE, (page - 1) * PAGE_SIZE),
  ]);
  if (!source || !documents) notFound();
  const canManage = org.role === "OWNER" || org.role === "ADMIN";
  const pages = Math.max(1, Math.ceil(documents.total / PAGE_SIZE));

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 space-y-8 p-8">
      <AppHeader current="knowledge" />
      <div className="space-y-4">
        <Link href="/knowledge" className="text-sm text-zinc-500 underline">
          ← All sources
        </Link>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">{source.name}</h1>
            {source.url ? (
              <a
                href={source.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-zinc-500 underline"
              >
                {source.url}
              </a>
            ) : null}
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
        {canManage ? <SourceActions sourceId={source.id} status={source.status} /> : null}
      </div>

      <section aria-label="Discovered pages" className="space-y-3">
        <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
          Discovered pages ({documents.total})
        </h2>
        {documents.items.length === 0 ? (
          <p className="rounded-lg border border-dashed border-zinc-300 p-6 text-center text-sm text-zinc-500 dark:border-zinc-700">
            {isInProgress(source.status)
              ? "The crawl is running. Pages will appear here as they are indexed."
              : "No pages were stored."}
          </p>
        ) : (
          <ul className="divide-y divide-zinc-200 rounded-lg border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
            {documents.items.map((doc) => (
              <li key={doc.id} data-document-id={doc.id} className="space-y-1 px-4 py-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <Link
                    href={`/knowledge/${source.id}/documents/${doc.id}`}
                    className="font-medium underline-offset-4 hover:underline"
                  >
                    {doc.title ?? doc.url}
                  </Link>
                  <span className="flex items-center gap-3 text-xs text-zinc-500">
                    <span>{doc.chunk_count} chunks</span>
                    <span>{doc.character_count.toLocaleString("en-US")} chars</span>
                    <StatusBadge status={doc.status} />
                  </span>
                </div>
                <p className="break-all text-xs text-zinc-500">{doc.url}</p>
                <p className="line-clamp-2 text-sm text-zinc-600 dark:text-zinc-400">
                  {doc.content_preview}
                </p>
              </li>
            ))}
          </ul>
        )}
        {pages > 1 ? (
          <nav aria-label="Pagination" className="flex items-center justify-between text-sm">
            {page > 1 ? (
              <Link href={`/knowledge/${source.id}?page=${page - 1}`} className="underline">
                ← Previous
              </Link>
            ) : (
              <span />
            )}
            <span className="text-zinc-500">
              Page {page} of {pages}
            </span>
            {page < pages ? (
              <Link href={`/knowledge/${source.id}?page=${page + 1}`} className="underline">
                Next →
              </Link>
            ) : (
              <span />
            )}
          </nav>
        ) : null}
      </section>
      <AutoRefresh active={isInProgress(source.status)} />
    </main>
  );
}
