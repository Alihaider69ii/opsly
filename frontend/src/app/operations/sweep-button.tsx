"use client";

import { useState, useTransition } from "react";
import { runSweepNow } from "./actions";

export function SweepButton() {
  const [pending, startTransition] = useTransition();
  const [message, setMessage] = useState<string | null>(null);

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        disabled={pending}
        data-testid="run-sweep"
        onClick={() =>
          startTransition(async () => {
            setMessage(null);
            const result = await runSweepNow();
            setMessage(
              result.ok
                ? `Sweep complete: ${result.data.created} new, ${result.data.updated} updated, ${result.data.breached} newly breached.`
                : result.state?.error ?? "Sweep failed.",
            );
          })
        }
        className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
      >
        {pending ? "Running detectors…" : "Run detection sweep"}
      </button>
      {message ? <p className="text-xs text-zinc-500">{message}</p> : null}
    </div>
  );
}
