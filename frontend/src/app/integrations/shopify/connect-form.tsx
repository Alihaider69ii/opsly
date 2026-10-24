"use client";

import { useActionState } from "react";
import { FormError } from "@/components/form-ui";
import { connectShopify } from "./actions";

export function ConnectForm({
  defaultShop,
  submitLabel = "Connect Shopify",
}: {
  defaultShop?: string;
  submitLabel?: string;
}) {
  const [state, action, pending] = useActionState(connectShopify, null);
  const fieldError = state?.fieldErrors?.shop;

  return (
    <form action={action} className="space-y-3">
      {state?.error && !fieldError ? <FormError message={state.error} /> : null}
      <div className="flex flex-col gap-2 sm:flex-row">
        <div className="flex-1">
          <label htmlFor="shop" className="sr-only">
            Shopify store address
          </label>
          <input
            id="shop"
            name="shop"
            type="text"
            inputMode="url"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            placeholder="your-store.myshopify.com"
            defaultValue={state?.values?.shop ?? defaultShop}
            aria-invalid={fieldError ? true : undefined}
            aria-describedby={fieldError ? "shop-error" : undefined}
            className="w-full rounded-md border border-zinc-300 bg-transparent px-3 py-2 text-sm outline-none focus:border-zinc-900 aria-[invalid=true]:border-red-500 dark:border-zinc-700 dark:focus:border-zinc-300"
          />
          {fieldError ? (
            <p id="shop-error" className="mt-1 text-sm text-red-600">
              {fieldError}
            </p>
          ) : null}
        </div>
        <button
          type="submit"
          disabled={pending}
          className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-60 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          {pending ? "Redirecting to Shopify…" : submitLabel}
        </button>
      </div>
    </form>
  );
}
