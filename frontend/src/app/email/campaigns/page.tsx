import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { StatusBadge } from "@/components/status-badge";
import { getCurrentOrganization, getEmailCampaigns, getEmailTemplates, requireUser } from "@/lib/dal";
import { formatDateTime } from "@/lib/format";
import { CampaignForm } from "./campaign-form";

export const metadata = { title: "Email campaigns · BizFlow AI" };

export default function CampaignsPage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading campaigns…</p>}>
      <Campaigns />
    </Suspense>
  );
}

async function Campaigns() {
  await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");
  const canManage = org.role === "OWNER" || org.role === "ADMIN";
  const [campaigns, templates] = await Promise.all([getEmailCampaigns(), getEmailTemplates()]);
  const activeTemplates = templates.filter((t) => t.status === "ACTIVE");

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 space-y-8 p-8">
      <AppHeader current="email" />
      <div>
        <p className="text-sm text-zinc-500">
          <Link href="/email" className="hover:underline">
            Email
          </Link>{" "}
          · Campaigns
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">Email campaigns</h1>
      </div>

      <section aria-label="Campaigns" className="space-y-2">
        <ul className="space-y-2" data-testid="campaign-list">
          {campaigns.map((campaign) => (
            <li
              key={campaign.id}
              data-campaign={campaign.name}
              className="flex items-center justify-between gap-2 rounded-lg border border-zinc-200 p-3 text-sm dark:border-zinc-800"
            >
              <Link href={`/email/campaigns/${campaign.id}`} className="hover:underline">
                {campaign.name}
              </Link>
              <span className="flex items-center gap-2">
                <span className="text-zinc-500">
                  {campaign.sent_count}/{campaign.total_recipients} sent
                </span>
                <StatusBadge status={campaign.status} />
                <span className="text-xs text-zinc-500">{formatDateTime(campaign.updated_at)}</span>
              </span>
            </li>
          ))}
          {campaigns.length === 0 ? <li className="text-sm text-zinc-500">No campaigns yet.</li> : null}
        </ul>
      </section>

      {canManage ? (
        <section aria-label="New campaign" className="space-y-3 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
          <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">New campaign</h2>
          <CampaignForm templates={activeTemplates} />
        </section>
      ) : null}
    </main>
  );
}
