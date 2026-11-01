"use client";

import { useState, useTransition } from "react";
import { StatusBadge } from "@/components/status-badge";
import { askOperationsAI } from "../actions";
import type { AIQueryResult } from "@/lib/types";

type Turn = { question: string; result: AIQueryResult } | { question: string; error: string };

const EXAMPLES = [
  "What orders need attention today?",
  "Which shipments are delayed?",
  "Which customers have repeated RTO?",
  "What inventory is at risk?",
  "What are today's biggest operational risks?",
];

export function AIChat() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, startTransition] = useTransition();

  function ask(question: string) {
    const trimmed = question.trim();
    if (!trimmed) return;
    setDraft("");
    startTransition(async () => {
      const result = await askOperationsAI(trimmed);
      if (!result.ok) {
        setTurns((prev) => [...prev, { question: trimmed, error: result.state?.error ?? "Something went wrong." }]);
        return;
      }
      setTurns((prev) => [...prev, { question: trimmed, result: result.data }]);
    });
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            disabled={pending}
            onClick={() => ask(example)}
            className="rounded-md border border-zinc-300 px-2 py-1 text-xs hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:hover:bg-zinc-900"
          >
            {example}
          </button>
        ))}
      </div>

      <ul className="space-y-4" data-testid="ai-turns">
        {turns.map((turn, index) => (
          <li key={index} className="space-y-2 rounded-lg border border-zinc-200 p-4 text-sm dark:border-zinc-800">
            <p className="font-medium">{turn.question}</p>
            {"error" in turn ? (
              <p className="text-red-600">{turn.error}</p>
            ) : (
              <>
                <p data-testid="ai-summary">{turn.result.summary}</p>
                {!turn.result.grounded ? (
                  <p className="text-xs text-amber-700 dark:text-amber-400">
                    No operational data matched this question -- answer refused rather than guessed.
                  </p>
                ) : (
                  <>
                    <p className="text-xs text-zinc-500">{turn.result.reasoning_summary}</p>
                    {turn.result.recommendation ? (
                      <p>
                        <span className="text-zinc-500">Recommendation: </span>
                        {turn.result.recommendation}
                      </p>
                    ) : null}
                    <div className="flex items-center gap-2 text-xs">
                      {turn.result.risk ? <StatusBadge status={turn.result.risk} /> : null}
                      {turn.result.required_approval ? (
                        <span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-800 dark:bg-amber-950 dark:text-amber-300">
                          Requires human approval
                        </span>
                      ) : null}
                      <span className="text-zinc-500">{turn.result.evidence.length} evidence record(s)</span>
                    </div>
                    {turn.result.evidence.length > 0 ? (
                      <details>
                        <summary className="cursor-pointer text-xs text-zinc-500">Evidence</summary>
                        <pre className="mt-1 overflow-x-auto text-xs">{JSON.stringify(turn.result.evidence, null, 2)}</pre>
                      </details>
                    ) : null}
                  </>
                )}
              </>
            )}
          </li>
        ))}
      </ul>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          ask(draft);
        }}
        className="flex gap-2"
      >
        <input
          aria-label="Ask Operations AI"
          data-testid="ai-question-input"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Ask about orders, shipments, inventory, refunds…"
          className="flex-1 rounded-md border border-zinc-300 px-3 py-2 text-sm dark:border-zinc-700 dark:bg-zinc-950"
        />
        <button
          type="submit"
          disabled={pending || !draft.trim()}
          data-testid="ai-ask-button"
          className="rounded-md bg-zinc-900 px-3 py-2 text-sm text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
        >
          {pending ? "Asking…" : "Ask"}
        </button>
      </form>
    </div>
  );
}
