"use client";

import { useState, useTransition } from "react";
import { StatusBadge } from "@/components/status-badge";
import { claimHandoff, resolveHandoff } from "../../actions";
import type { WhatsAppConversation } from "@/lib/types";
import { formatDateTime } from "@/lib/format";

export function HandoffPanel({
  conversationId,
  initial,
  canManage,
}: {
  conversationId: string;
  initial: WhatsAppConversation;
  canManage: boolean;
}) {
  const [conversation, setConversation] = useState(initial);
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  function claim() {
    setError(null);
    startTransition(async () => {
      const result = await claimHandoff(conversationId);
      if (!result.ok) setError(result.state?.error ?? "Something went wrong.");
      else setConversation(result.data);
    });
  }

  function resolve() {
    setError(null);
    startTransition(async () => {
      const result = await resolveHandoff(conversationId);
      if (!result.ok) setError(result.state?.error ?? "Something went wrong.");
      else setConversation(result.data);
    });
  }

  if (conversation.ai_state === "AI_ACTIVE") {
    return (
      <p data-testid="handoff-status" className="text-sm text-zinc-500">
        AI is answering this conversation.
      </p>
    );
  }

  return (
    <div data-testid="handoff-status" className="space-y-2">
      <div className="flex items-center justify-between">
        <StatusBadge status={conversation.ai_state} />
        {conversation.handoff_at ? (
          <span className="text-xs text-zinc-500">
            since {formatDateTime(conversation.handoff_at)}
          </span>
        ) : null}
      </div>
      {conversation.handoff_reason ? (
        <p className="text-sm text-zinc-500">
          Reason: <span data-field="handoff-reason">{conversation.handoff_reason}</span>
        </p>
      ) : null}
      {canManage ? (
        <div className="flex gap-2">
          {conversation.ai_state === "HUMAN_REQUIRED" ? (
            <button
              type="button"
              disabled={pending}
              onClick={claim}
              className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
            >
              Claim
            </button>
          ) : null}
          <button
            type="button"
            disabled={pending}
            onClick={resolve}
            className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
          >
            Resolve (return to AI)
          </button>
        </div>
      ) : null}
      {error ? (
        <p role="alert" className="text-sm text-red-600">
          {error}
        </p>
      ) : null}
    </div>
  );
}
