"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { cancelTask, claimTask, escalateTask, releaseTask, resolveTask } from "../actions";
import type { OperationalTask } from "@/lib/types";

// A list row never keeps its own copy of server state: every action just asks the router to
// refresh, so the backend's answer is always what renders (no stale-state drift like a client
// component that duplicated the list into useState would risk).
export function TaskActions({ task }: { task: OperationalTask }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  function run(fn: () => Promise<{ ok: boolean; state?: { error?: string } | null }>) {
    setError(null);
    startTransition(async () => {
      const result = await fn();
      if (!result.ok) {
        setError(result.state?.error ?? "Something went wrong.");
        return;
      }
      router.refresh();
    });
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      {error ? <span className="text-xs text-red-600">{error}</span> : null}
      {task.status === "OPEN" ? (
        <Button label="Claim" disabled={pending} onClick={() => run(() => claimTask(task.id))} />
      ) : null}
      {task.status === "ASSIGNED" || task.status === "IN_PROGRESS" ? (
        <>
          <Button label="Release" disabled={pending} onClick={() => run(() => releaseTask(task.id))} />
          <Button label="Escalate" disabled={pending} onClick={() => run(() => escalateTask(task.id))} />
          <Button label="Resolve" disabled={pending} onClick={() => run(() => resolveTask(task.id))} />
        </>
      ) : null}
      {task.status !== "RESOLVED" && task.status !== "CANCELLED" ? (
        <Button label="Cancel" disabled={pending} onClick={() => run(() => cancelTask(task.id))} />
      ) : null}
    </div>
  );
}

function Button({ label, onClick, disabled }: { label: string; onClick: () => void; disabled: boolean }) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      data-action={label.toLowerCase()}
      className="rounded-md border border-zinc-300 px-2 py-1 text-xs hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
    >
      {label}
    </button>
  );
}
