import { Suspense } from "react";
import Link from "next/link";
import { notFound, redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { StatusBadge } from "@/components/status-badge";
import { getCurrentOrganization, getEmailCampaign, requireUser } from "@/lib/dal";
import { formatDateTime, isUuid } from "@/lib/format";
import { CampaignActions } from "./campaign-actions";

export const metadata = { title: "Campaign · BizFlow AI" };

export default function CampaignPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading campaign…</p>}>
      <Campaign params={params} />
    </Suspense>
  );
}

async function Campaign({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!isUuid(id)) notFound();
  await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");
  const canManage = org.role === "OWNER" || org.role === "ADMIN";
  const campaign = await getEmailCampaign(id);
  if (!campaign) notFound();

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 space-y-8 p-8">
      <AppHeader current="email" />
      <div>
        <p className="text-sm text-zinc-500">
          <Link href="/email/campaigns" className="hover:underline">
            Campaigns
          </Link>{" "}
          · {campaign.name}
        </p>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          {campaign.name}
          <StatusBadge status={campaign.status} />
        </h1>
      </div>

      <section
        aria-label="Progress"
        data-testid="campaign-progress"
        className="grid grid-cols-2 gap-3 sm:grid-cols-4"
      >
        <Stat label="Recipients" value={campaign.total_recipients} />
        <Stat label="Sent" value={campaign.sent_count} />
        <Stat label="Failed" value={campaign.failed_count} />
        <Stat label="Skipped" value={campaign.skipped_count} />
      </section>

      <dl className="grid gap-x-6 gap-y-1 rounded-lg border border-zinc-200 p-4 text-sm sm:grid-cols-2 dark:border-zinc-800">
        <Fact label="Started" value={formatDateTime(campaign.started_at)} />
        <Fact label="Completed" value={formatDateTime(campaign.completed_at)} />
      </dl>

      {canManage ? (
        <CampaignActions key={`${campaign.id}-${campaign.updated_at}`} initial={campaign} />
      ) : null}
    </main>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border border-zinc-200 p-3 dark:border-zinc-800">
      <p className="text-2xl font-semibold">{value}</p>
      <p className="text-xs text-zinc-500">{label}</p>
    </div>
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
