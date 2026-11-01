"use client";

import { useState, useTransition } from "react";
import type { EmailCampaign } from "@/lib/types";
import { cancelCampaign, pauseCampaign, resumeCampaign, startCampaign } from "../../actions";

const button =
  "rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900";

// A started campaign finishes asynchronously in the worker (see services/email/campaigns.py).
// The parent page gives this component key={`${id}-${updated_at}`}, so React remounts it (reset
// local state to the fresh `initial`) whenever the server's view of the campaign actually
// changes, instead of the buttons keeping the pre-refresh status indefinitely.
export function CampaignActions({ initial }: { initial: EmailCampaign }) {
  const [campaign, setCampaign] = useState(initial);
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  function run(fn: () => ReturnType<typeof startCampaign>) {
    setError(null);
    startTransition(async () => {
      const result = await fn();
      if (!result.ok) {
        setError(result.state?.error ?? "Something went wrong.");
        return;
      }
      setCampaign(result.data);
    });
  }

  return (
    <div className="space-y-2" data-testid="campaign-actions">
      <div className="flex flex-wrap gap-2">
        {campaign.status === "DRAFT" || campaign.status === "SCHEDULED" ? (
          <button type="button" disabled={pending} onClick={() => run(() => startCampaign(campaign.id))} className={button}>
            Start sending
          </button>
        ) : null}
        {campaign.status === "RUNNING" ? (
          <button type="button" disabled={pending} onClick={() => run(() => pauseCampaign(campaign.id))} className={button}>
            Pause
          </button>
        ) : null}
        {campaign.status === "PAUSED" ? (
          <button type="button" disabled={pending} onClick={() => run(() => resumeCampaign(campaign.id))} className={button}>
            Resume
          </button>
        ) : null}
        {campaign.status !== "COMPLETED" && campaign.status !== "CANCELLED" ? (
          <button type="button" disabled={pending} onClick={() => run(() => cancelCampaign(campaign.id))} className={button}>
            Cancel
          </button>
        ) : null}
      </div>
      {error ? (
        <p role="alert" className="text-sm text-red-600">
          {error}
        </p>
      ) : null}
    </div>
  );
}
