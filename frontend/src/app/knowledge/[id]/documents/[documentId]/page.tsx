import { Suspense } from "react";
import Link from "next/link";
import { notFound } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { StatusBadge } from "@/components/status-badge";
import { getSourceDocument, requireUser } from "@/lib/dal";
import { formatDateTime, isUuid } from "@/lib/format";

export const metadata = { title: "Page · Knowledge Base · BizFlow AI" };

type Props = { params: Promise<{ id: string; documentId: string }> };

export default function DocumentPage({ params }: Props) {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading…</p>}>
      <Document params={params} />
    </Suspense>
  );
}

async function Document({ params }: Props) {
  const { id, documentId } = await params;
  if (!isUuid(id) || !isUuid(documentId)) notFound();
  await requireUser();
  const doc = await getSourceDocument(id, documentId);
  if (!doc) notFound();

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 space-y-6 p-8">
      <AppHeader current="knowledge" />
      <Link href={`/knowledge/${id}`} className="text-sm text-zinc-500 underline">
        ← Back to source
      </Link>
      <div className="space-y-2">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <h1 className="text-2xl font-semibold tracking-tight">{doc.title ?? doc.url}</h1>
          <StatusBadge status={doc.status} />
        </div>
        <a
          href={doc.url}
          target="_blank"
          rel="noopener noreferrer"
          className="break-all text-sm text-zinc-500 underline"
        >
          {doc.url}
        </a>
        <p className="text-sm text-zinc-500">
          {doc.chunk_count} chunks · {doc.character_count.toLocaleString("en-US")} characters ·
          updated {formatDateTime(doc.updated_at)}
        </p>
      </div>
      {/* Crawled text is untrusted: it is only ever rendered as text, never as HTML. */}
      <pre className="whitespace-pre-wrap break-words rounded-lg border border-zinc-200 p-4 font-sans text-sm leading-relaxed dark:border-zinc-800">
        {doc.content}
      </pre>
    </main>
  );
}
