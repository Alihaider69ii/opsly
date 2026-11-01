"use client";

import { useState } from "react";
import type { WhatsAppAIConfidence, WhatsAppConnection } from "@/lib/types";
import { setWhatsAppAISettings } from "./actions";

const button =
  "rounded-md border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900";
const input =
  "w-full rounded-md border border-zinc-300 bg-transparent px-2 py-1.5 text-sm dark:border-zinc-700";

export function AISettingsForm({ connection }: { connection: WhatsAppConnection }) {
  const [groundingRequired, setGroundingRequired] = useState(connection.ai_grounding_required);
  const [minConfidence, setMinConfidence] = useState<WhatsAppAIConfidence>(
    connection.ai_min_confidence,
  );
  const [handoffEnabled, setHandoffEnabled] = useState(connection.ai_handoff_enabled);
  const [fallbackMessage, setFallbackMessage] = useState(connection.ai_fallback_message ?? "");
  const [handoffMessage, setHandoffMessage] = useState(connection.ai_handoff_message ?? "");
  const [maxReplies, setMaxReplies] = useState(connection.ai_max_replies_per_window);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function save() {
    setPending(true);
    setError(null);
    setMessage(null);
    const result = await setWhatsAppAISettings({
      grounding_required: groundingRequired,
      min_confidence: minConfidence,
      handoff_enabled: handoffEnabled,
      fallback_message: fallbackMessage.trim() || null,
      handoff_message: handoffMessage.trim() || null,
      max_replies_per_window: maxReplies,
    });
    if (result?.error) setError(result.error);
    else setMessage(result?.message ?? "Saved.");
    setPending(false);
  }

  return (
    <div data-testid="whatsapp-ai-settings" className="space-y-4 text-sm">
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={groundingRequired}
          onChange={(e) => setGroundingRequired(e.target.checked)}
        />
        Require business knowledge before answering (never guess facts)
      </label>

      <label className="block space-y-1">
        <span className="text-zinc-500">Minimum confidence to send a generated answer</span>
        <select
          value={minConfidence}
          onChange={(e) => setMinConfidence(e.target.value as WhatsAppAIConfidence)}
          className={input}
        >
          <option value="LOW">Low</option>
          <option value="MEDIUM">Medium</option>
          <option value="HIGH">High</option>
        </select>
      </label>

      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={handoffEnabled}
          onChange={(e) => setHandoffEnabled(e.target.checked)}
        />
        Hand off to a human when the AI can&apos;t help
      </label>

      <label className="block space-y-1">
        <span className="text-zinc-500">
          Max AI replies per customer-service window ({maxReplies === 0 ? "unlimited" : maxReplies})
        </span>
        <input
          type="number"
          min={0}
          max={1000}
          value={maxReplies}
          onChange={(e) => setMaxReplies(Number(e.target.value))}
          className={input}
        />
      </label>

      <label className="block space-y-1">
        <span className="text-zinc-500">Fallback message (shown when the AI isn&apos;t sure)</span>
        <textarea
          value={fallbackMessage}
          onChange={(e) => setFallbackMessage(e.target.value)}
          placeholder="I don't have enough information to confirm that. I can connect you with the team for help."
          rows={2}
          className={input}
        />
      </label>

      <label className="block space-y-1">
        <span className="text-zinc-500">Handoff message (shown when connecting to a human)</span>
        <textarea
          value={handoffMessage}
          onChange={(e) => setHandoffMessage(e.target.value)}
          placeholder="I'll connect you with our team so they can help with this."
          rows={2}
          className={input}
        />
      </label>

      <button type="button" onClick={save} disabled={pending} className={button}>
        {pending ? "Saving…" : "Save AI settings"}
      </button>

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
