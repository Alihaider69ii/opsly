"use client";

import { useActionState, useState } from "react";
import { disconnectShopify, syncNow } from "./actions";

const button =
  "rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900";

export function StoreActions({
  syncing,
  needsReconnect = false,
}: {
  syncing: boolean;
  needsReconnect?: boolean;
}) {
  const [syncState, syncAction, starting] = useActionState(syncNow, null);
  const [disconnectState, disconnectAction, disconnecting] = useActionState(
    disconnectShopify,
    null,
  );
  const [confirming, setConfirming] = useState(false);
  const error = syncState?.error ?? disconnectState?.error;
  const message = syncState?.message ?? disconnectState?.message;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <form action={syncAction}>
          <button
            type="submit"
            disabled={syncing || starting || needsReconnect}
            title={needsReconnect ? "Reconnect your store first" : undefined}
            className={button}
          >
            {starting ? "Starting…" : syncing ? "Sync in progress" : "Sync now"}
          </button>
        </form>
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
            Disconnect this store?
          </p>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            BizFlow will stop syncing, discard its saved access, and remove the products, customers
            and orders it imported. Nothing changes in your Shopify store, and you can connect it
            again later to re-import everything.
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
