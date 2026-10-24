"use client";

import { useActionState } from "react";
import { FormError, SubmitButton } from "@/components/form-ui";
import type { ChatWidget } from "@/lib/types";
import { saveWidgetSettings } from "./actions";

export function SettingsForm({ widget }: { widget: ChatWidget }) {
  const [state, action, pending] = useActionState(saveWidgetSettings, null);
  const values = state?.values;
  return (
    <form action={action} className="space-y-4">
      {state?.error ? <FormError message={state.error} /> : null}
      {state?.message ? (
        <p
          role="status"
          className="rounded-md border border-green-300 bg-green-50 px-3 py-2 text-sm text-green-800 dark:border-green-900 dark:bg-green-950 dark:text-green-300"
        >
          {state.message}
        </p>
      ) : null}

      <label className="flex items-center gap-2 text-sm font-medium">
        <input type="checkbox" name="enabled" defaultChecked={widget.enabled} />
        Chat widget enabled
      </label>

      <div className="space-y-1.5">
        <label htmlFor="welcome_message" className="block text-sm font-medium">
          Welcome message
        </label>
        <input
          id="welcome_message"
          name="welcome_message"
          maxLength={500}
          defaultValue={values?.welcome_message ?? widget.welcome_message}
          className="w-full rounded-md border border-zinc-300 bg-transparent px-3 py-2 text-sm dark:border-zinc-700"
        />
      </div>

      <div className="space-y-1.5">
        <label htmlFor="allowed_origins" className="block text-sm font-medium">
          Allowed websites (optional)
        </label>
        <textarea
          id="allowed_origins"
          name="allowed_origins"
          rows={3}
          placeholder={"https://yourbrand.com\nhttps://www.yourbrand.com"}
          defaultValue={values?.allowed_origins ?? widget.allowed_origins.join("\n")}
          aria-describedby="origins-hint"
          className="w-full rounded-md border border-zinc-300 bg-transparent px-3 py-2 font-mono text-sm dark:border-zinc-700"
        />
        <p id="origins-hint" className="text-sm text-zinc-500">
          One per line. Leave empty to allow the widget on any site. This stops other websites from
          embedding your chat in a browser; it is not a substitute for rate limits.
        </p>
      </div>
      <SubmitButton pending={pending}>Save settings</SubmitButton>
    </form>
  );
}
