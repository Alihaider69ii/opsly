"use client";

import { useActionState, useEffect, useRef } from "react";
import { FormError } from "@/components/form-ui";
import { addWebsite } from "./actions";

export function AddWebsiteForm() {
  const [state, action, pending] = useActionState(addWebsite, null);
  const formRef = useRef<HTMLFormElement>(null);

  useEffect(() => {
    if (state?.message) formRef.current?.reset();
  }, [state]);

  return (
    <form ref={formRef} action={action} className="space-y-3">
      {state?.error ? <FormError message={state.error} /> : null}
      {state?.message ? (
        <p
          role="status"
          className="rounded-md border border-green-300 bg-green-50 px-3 py-2 text-sm text-green-800 dark:border-green-900 dark:bg-green-950 dark:text-green-300"
        >
          {state.message}
        </p>
      ) : null}
      <div className="flex flex-col gap-2 sm:flex-row">
        <div className="flex-1">
          <label htmlFor="url" className="sr-only">
            Website address
          </label>
          <input
            id="url"
            name="url"
            type="text"
            inputMode="url"
            autoComplete="url"
            placeholder="https://yourbrand.com"
            defaultValue={state?.values?.url}
            aria-invalid={state?.fieldErrors?.url ? true : undefined}
            aria-describedby={state?.fieldErrors?.url ? "url-error" : undefined}
            className="w-full rounded-md border border-zinc-300 bg-transparent px-3 py-2 text-sm outline-none focus:border-zinc-900 aria-[invalid=true]:border-red-500 dark:border-zinc-700 dark:focus:border-zinc-300"
          />
          {state?.fieldErrors?.url ? (
            <p id="url-error" className="mt-1 text-sm text-red-600">
              {state.fieldErrors.url}
            </p>
          ) : null}
        </div>
        <button
          type="submit"
          disabled={pending}
          className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-60 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          {pending ? "Adding…" : "+ Add Website"}
        </button>
      </div>
      <p className="text-sm text-zinc-500">
        Only public websites are crawled, and the site&apos;s robots.txt is respected.
      </p>
    </form>
  );
}
