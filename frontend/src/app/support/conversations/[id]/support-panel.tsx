"use client";

import { useState, useTransition } from "react";
import { PriorityBadge } from "@/components/priority-badge";
import { StatusBadge } from "@/components/status-badge";
import {
  claimConversation,
  getConversationSummary,
  releaseConversation,
  replyToConversation,
  requestHumanForConversation,
  reopenConversation,
  resolveConversation,
  returnConversationToAi,
  setConversationPriority,
  takeOverConversation,
} from "../../actions";
import type { SupportConversationSummary, SupportMessage, SupportPriority } from "@/lib/types";
import { formatDateTime } from "@/lib/format";

const PRIORITIES: SupportPriority[] = ["URGENT", "HIGH", "NORMAL", "LOW"];

export function SupportPanel({
  conversationId,
  initial,
  initialMessages,
}: {
  conversationId: string;
  initial: SupportConversationSummary;
  initialMessages: SupportMessage[];
}) {
  const [conversation, setConversation] = useState(initial);
  const [messages, setMessages] = useState(initialMessages);
  const [draft, setDraft] = useState("");
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  function run(fn: () => Promise<{ ok: true; data: unknown } | { ok: false; state: { error?: string } | null }>) {
    setError(null);
    startTransition(async () => {
      const result = await fn();
      if (!result.ok) {
        setError(result.state?.error ?? "Something went wrong.");
        return;
      }
      const data = result.data as { conversation?: SupportConversationSummary };
      if (data.conversation) setConversation(data.conversation);
    });
  }

  function send() {
    const content = draft.trim();
    if (!content) return;
    setError(null);
    startTransition(async () => {
      const result = await replyToConversation(conversationId, content);
      if (!result.ok) {
        setError(result.state?.error ?? "Something went wrong.");
        return;
      }
      setMessages((prev) => [...prev, result.data]);
      setDraft("");
      // A reply implicitly takes the conversation over -- re-sync status/ai_state/assignment
      // rather than leaving the badges and action buttons showing the pre-reply state.
      const refreshed = await getConversationSummary(conversationId);
      if (refreshed.ok) setConversation(refreshed.data);
    });
  }

  const resolved = conversation.status === "RESOLVED" || conversation.status === "CLOSED";

  return (
    <div className="space-y-6">
      <div
        data-testid="support-status"
        className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800"
      >
        <div className="flex items-center gap-2">
          <StatusBadge status={conversation.channel} />
          <StatusBadge status={conversation.status} />
          <PriorityBadge priority={conversation.priority} />
        </div>
        <div className="flex flex-wrap gap-2">
          {conversation.ai_state === "AI_ACTIVE" ? (
            <ActionButton label="Take over" onClick={() => run(() => takeOverConversation(conversationId))} disabled={pending} />
          ) : (
            <ActionButton label="Return to AI" onClick={() => run(() => returnConversationToAi(conversationId))} disabled={pending} />
          )}
          {conversation.ai_state === "AI_ACTIVE" ? (
            <ActionButton
              label="Request human"
              onClick={() => run(() => requestHumanForConversation(conversationId, "dashboard_requested"))}
              disabled={pending}
            />
          ) : null}
          {conversation.assigned_agent_user_id ? (
            <ActionButton label="Release" onClick={() => run(() => releaseConversation(conversationId))} disabled={pending} />
          ) : (
            <ActionButton label="Claim" onClick={() => run(() => claimConversation(conversationId))} disabled={pending} />
          )}
          {resolved ? (
            <ActionButton label="Reopen" onClick={() => run(() => reopenConversation(conversationId))} disabled={pending} />
          ) : (
            <ActionButton label="Resolve" onClick={() => run(() => resolveConversation(conversationId))} disabled={pending} />
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 text-sm">
        <span className="text-zinc-500">Priority:</span>
        {PRIORITIES.map((p) => (
          <button
            key={p}
            type="button"
            disabled={pending}
            data-priority-option={p}
            onClick={() => run(() => setConversationPriority(conversationId, p))}
            className={`rounded-md px-2 py-1 text-xs ${
              conversation.priority === p
                ? "ring-1 ring-zinc-900 dark:ring-zinc-100"
                : "hover:bg-zinc-100 dark:hover:bg-zinc-900"
            }`}
          >
            <PriorityBadge priority={p} />
          </button>
        ))}
      </div>

      {error ? (
        <p role="alert" className="text-sm text-red-600">
          {error}
        </p>
      ) : null}

      <ul className="space-y-2" data-testid="support-messages">
        {messages.map((message) => (
          <li
            key={message.id}
            data-message={message.id}
            data-sender-type={message.sender_type ?? "unknown"}
            className={`max-w-[85%] space-y-1 rounded-lg border p-3 text-sm ${
              message.direction === "OUTBOUND"
                ? "ml-auto border-blue-200 bg-blue-50 dark:border-blue-900 dark:bg-blue-950"
                : "border-zinc-200 dark:border-zinc-800"
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-zinc-500">{formatDateTime(message.created_at)}</span>
              {message.sender_type ? <StatusBadge status={message.sender_type} /> : null}
            </div>
            <p className="whitespace-pre-wrap">{message.content ?? "(no text content)"}</p>
            {message.error_code ? (
              <p className="text-xs text-red-700 dark:text-red-400">{message.error_code}</p>
            ) : null}
          </li>
        ))}
        {messages.length === 0 ? <li className="text-sm text-zinc-500">No messages yet.</li> : null}
      </ul>

      <div className="space-y-2">
        <textarea
          aria-label="Reply"
          data-testid="reply-textarea"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          rows={3}
          className="w-full rounded-md border border-zinc-300 p-2 text-sm dark:border-zinc-700 dark:bg-zinc-950"
          placeholder="Reply as a human. This takes over the conversation from AI."
        />
        <button
          type="button"
          disabled={pending || !draft.trim()}
          onClick={send}
          data-testid="send-reply"
          className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
        >
          Send reply
        </button>
      </div>
    </div>
  );
}

function ActionButton({
  label,
  onClick,
  disabled,
}: {
  label: string;
  onClick: () => void;
  disabled: boolean;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      data-action={label.toLowerCase().replace(/\s+/g, "-")}
      className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
    >
      {label}
    </button>
  );
}
