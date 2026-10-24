"use client";

import { useActionState, useState } from "react";
import type { SourceStatus } from "@/lib/types";
import { deleteSource, recrawl } from "./actions";

const button =
  "rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900";

export function SourceActions({ sourceId, status }: { sourceId: string; status: SourceStatus }) {
  const [crawlState, crawlAction, crawling] = useActionState(recrawl, null);
  const [deleteState, deleteAction, deleting] = useActionState(deleteSource, null);
  const [confirming, setConfirming] = useState(false);
  const busy = status === "PENDING" || status === "CRAWLING";
  const error = crawlState?.error ?? deleteState?.error;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <form action={crawlAction}>
          <input type="hidden" name="source_id" value={sourceId} />
          <button type="submit" disabled={busy || crawling} className={button}>
            {crawling ? "Queuing…" : "Re-crawl"}
          </button>
        </form>

        {confirming ? (
          <form action={deleteAction} className="flex items-center gap-2">
            <input type="hidden" name="source_id" value={sourceId} />
            <span className="text-sm text-red-700 dark:text-red-400">
              Delete this source and all its pages?
            </span>
            <button
              type="submit"
              disabled={deleting}
              className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-60"
            >
              {deleting ? "Deleting…" : "Yes, delete"}
            </button>
            <button type="button" onClick={() => setConfirming(false)} className={button}>
              Cancel
            </button>
          </form>
        ) : (
          <button type="button" onClick={() => setConfirming(true)} className={button}>
            Delete
          </button>
        )}
      </div>
      {error ? (
        <p role="alert" className="text-sm text-red-600">
          {error}
        </p>
      ) : null}
    </div>
  );
}
