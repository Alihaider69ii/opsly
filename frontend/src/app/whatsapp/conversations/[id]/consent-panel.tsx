"use client";

import { useState, useTransition } from "react";
import { StatusBadge } from "@/components/status-badge";
import { optOutContact, setConsent } from "../../actions";
import type { WhatsAppConsentCategory, WhatsAppConsentList } from "@/lib/types";

const CATEGORIES: WhatsAppConsentCategory[] = ["TRANSACTIONAL", "MARKETING", "SUPPORT"];

export function ConsentPanel({
  contactId,
  initial,
  canManage,
}: {
  contactId: string;
  initial: WhatsAppConsentList;
  canManage: boolean;
}) {
  const [consent, setConsentState] = useState(initial);
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  function update(category: WhatsAppConsentCategory, status: "OPTED_IN" | "OPTED_OUT") {
    setError(null);
    startTransition(async () => {
      const result = await setConsent(contactId, { category, status, source: "dashboard_manual" });
      if (!result.ok) setError(result.state?.error ?? "Something went wrong.");
      else setConsentState(result.data);
    });
  }

  function optOut() {
    setError(null);
    startTransition(async () => {
      const result = await optOutContact(contactId, "dashboard_manual");
      if (!result.ok) setError(result.state?.error ?? "Something went wrong.");
      else setConsentState(result.data);
    });
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-sm text-zinc-500">Suppressed (blocks every category)</span>
        <span
          className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-medium ${
            consent.suppressed
              ? "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300"
              : "bg-zinc-100 text-zinc-700 dark:bg-zinc-900 dark:text-zinc-300"
          }`}
        >
          {consent.suppressed ? "Yes" : "No"}
        </span>
      </div>
      <ul className="space-y-2">
        {CATEGORIES.map((category) => {
          const row = consent.consents.find((c) => c.category === category);
          const status = row?.status ?? null;
          return (
            <li key={category} className="flex items-center justify-between gap-2 text-sm">
              <span>{category}</span>
              <span className="flex items-center gap-2">
                {status === null ? (
                  <span className="text-xs text-zinc-500">Unknown</span>
                ) : (
                  <StatusBadge status={status} />
                )}
                {canManage ? (
                  <>
                    <button
                      type="button"
                      disabled={pending || status === "OPTED_IN"}
                      onClick={() => update(category, "OPTED_IN")}
                      className="rounded-md border border-zinc-300 px-2 py-1 text-xs hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
                    >
                      Opt in
                    </button>
                    <button
                      type="button"
                      disabled={pending || status === "OPTED_OUT"}
                      onClick={() => update(category, "OPTED_OUT")}
                      className="rounded-md border border-zinc-300 px-2 py-1 text-xs hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
                    >
                      Opt out
                    </button>
                  </>
                ) : null}
              </span>
            </li>
          );
        })}
      </ul>
      {canManage && !consent.suppressed ? (
        <button
          type="button"
          disabled={pending}
          onClick={optOut}
          className="rounded-md border border-red-300 px-3 py-1.5 text-sm text-red-700 hover:bg-red-50 disabled:opacity-50 dark:border-red-900 dark:text-red-400 dark:hover:bg-red-950"
        >
          Suppress (block all future messages)
        </button>
      ) : null}
      {error ? (
        <p role="alert" className="text-sm text-red-600">
          {error}
        </p>
      ) : null}
    </div>
  );
}
