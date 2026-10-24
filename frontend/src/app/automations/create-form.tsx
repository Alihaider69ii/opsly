"use client";

import { useActionState } from "react";
import { createAutomation } from "./actions";

const initial = null;

export function CreateAutomationForm() {
  const [state, action, pending] = useActionState(createAutomation, initial);
  return (
    <form action={action} className="space-y-4 rounded-xl border border-zinc-200 p-5 dark:border-zinc-800">
      <h2 className="font-medium">Create a workflow</h2>
      <label className="block space-y-1 text-sm">Name<input required name="name" maxLength={160} className="w-full rounded-md border bg-transparent p-2" placeholder="Abandoned cart follow-up" /></label>
      <label className="block space-y-1 text-sm">Trigger<select name="event" className="w-full rounded-md border bg-transparent p-2">
        <option value="cart_abandoned">Cart abandoned</option><option value="order_created">Order created</option><option value="order_paid">Order paid</option><option value="order_cancelled">Order cancelled</option><option value="customer_created">Customer created</option><option value="website_lead_captured">Website lead captured</option><option value="website_conversation_started">Website conversation started</option><option value="conversation_message_received">Conversation message received</option>
      </select></label>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" name="condition" />Only when value exceeds threshold</label>
      <label className="block space-y-1 text-sm">Threshold<input name="threshold" type="number" min="0" step="0.01" className="w-full rounded-md border bg-transparent p-2" placeholder="1000" /></label>
      <label className="block space-y-1 text-sm">First action<select name="action" className="w-full rounded-md border bg-transparent p-2"><option value="log">Record an audit note</option><option value="create_task">Create a follow-up task</option><option value="ai_generate">Generate follow-up copy with AI</option><option value="wait">Wait before continuing</option></select></label>
      <label className="block space-y-1 text-sm">Action text / objective<input name="action_text" maxLength={1000} className="w-full rounded-md border bg-transparent p-2" placeholder="Write a friendly reminder" /></label>
      <label className="block space-y-1 text-sm">Wait seconds (used for Wait)<input name="seconds" type="number" min="1" max="2592000" defaultValue="1500" className="w-full rounded-md border bg-transparent p-2" /></label>
      {state?.error ? <p role="alert" className="text-sm text-red-700">{state.error}</p> : null}
      {state?.message ? <p role="status" className="text-sm text-emerald-700">{state.message}</p> : null}
      <button disabled={pending} className="rounded-md bg-zinc-900 px-4 py-2 text-sm text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900">{pending ? "Saving…" : "Save draft"}</button>
    </form>
  );
}
