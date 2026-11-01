import { Suspense } from "react";
import { notFound, redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { getCurrentOrganization, getOperationalIssue, requireUser } from "@/lib/dal";
import { formatDateTime, isUuid } from "@/lib/format";
import { IssuePanel } from "./issue-panel";

export const metadata = { title: "Issue · Operations · BizFlow AI" };

export default function IssuePage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading issue…</p>}>
      <Issue params={params} />
    </Suspense>
  );
}

async function Issue({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!isUuid(id)) notFound();
  const user = await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");

  const issue = await getOperationalIssue(id);
  if (!issue) notFound();

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 space-y-8 p-8">
      <AppHeader current="operations" />
      <div>
        <p className="text-sm text-zinc-500">
          <a href="/operations" className="hover:underline">
            Operations
          </a>{" "}
          · Issue
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">{issue.title}</h1>
        <p className="mt-1 text-xs text-zinc-500">Detected {formatDateTime(issue.created_at)} by {issue.detected_by}</p>
      </div>

      <IssuePanel key={`${issue.id}-${issue.updated_at}`} issueId={id} initial={issue} currentUserId={user.id} />
    </main>
  );
}
