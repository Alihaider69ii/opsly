"use client";

import { useActionState, useState } from "react";
import { disconnectWhatsApp, setWhatsAppAutoReply, syncWhatsAppTemplates } from "./actions";

const button =
  "rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900";

export function ConnectionActions({
  aiAutoReplyEnabled,
  canSyncTemplates,
}: {
  aiAutoReplyEnabled: boolean;
  canSyncTemplates: boolean;
}) {
  const [syncState, syncAction, syncing] = useActionState(syncWhatsAppTemplates, null);
  const [disconnectState, disconnectAction, disconnecting] = useActionState(
    disconnectWhatsApp,
    null,
  );
  const [confirming, setConfirming] = useState(false);
  const [autoReplyPending, setAutoReplyPending] = useState(false);
  const [autoReplyError, setAutoReplyError] = useState<string | null>(null);
  const error = syncState?.error ?? disconnectState?.error ?? autoReplyError;
  const message = syncState?.message ?? disconnectState?.message;

  async function toggleAutoReply() {
    setAutoReplyPending(true);
    setAutoReplyError(null);
    const result = await setWhatsAppAutoReply(!aiAutoReplyEnabled);
    if (result?.error) setAutoReplyError(result.error);
    setAutoReplyPending(false);
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {canSyncTemplates ? (
          <form action={syncAction}>
            <button type="submit" disabled={syncing} className={button}>
              {syncing ? "Syncing…" : "Sync templates"}
            </button>
          </form>
        ) : null}
        <button
          type="button"
          onClick={toggleAutoReply}
          disabled={autoReplyPending}
          className={button}
        >
          {aiAutoReplyEnabled ? "Disable AI auto-reply" : "Enable AI auto-reply"}
        </button>
        {confirming ? null : (
          <button type="button" onClick={() => setConfirming(true)} className={button}>
            Disconnect
          </button>
        )}
      </div>

      {confirming ? (
        <form
          action={disconnectAction}
          className="space-y-3 rounded-lg border border-red-300 p-4 dark:border-red-900"
        >
          <p className="text-sm font-medium text-red-700 dark:text-red-400">
            Disconnect this WhatsApp number?
          </p>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            BizFlow will stop sending and receiving WhatsApp messages. Conversation history, consent
            and suppression records are kept (they cannot be re-imported from Meta), so
            re-connecting later does not create duplicates or lose opt-out protection.
          </p>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={disconnecting}
              className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-60"
            >
              {disconnecting ? "Disconnecting…" : "Yes, disconnect"}
            </button>
            <button type="button" onClick={() => setConfirming(false)} className={button}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}

      {error ? (
        <p role="alert" className="text-sm text-red-600">
          {error}
        </p>
      ) : null}
      {message && !error ? (
        <p role="status" className="text-sm text-green-700 dark:text-green-400">
          {message}
        </p>
      ) : null}
    </div>
  );
}
