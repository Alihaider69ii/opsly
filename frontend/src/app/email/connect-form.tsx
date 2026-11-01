"use client";

import { useActionState } from "react";
import { Field, FormError, SubmitButton } from "@/components/form-ui";
import { connectEmail } from "./actions";

export function ConnectForm() {
  const [state, action, pending] = useActionState(connectEmail, null);

  return (
    <form action={action} className="space-y-3">
      {state?.error ? <FormError message={state.error} /> : null}
      <Field
        label="From address"
        name="from_address"
        type="email"
        autoComplete="off"
        defaultValue={state?.values?.from_address}
        error={state?.fieldErrors?.from_address}
        hint="The address customer support email is sent from."
      />
      <Field
        label="From name (optional)"
        name="from_name"
        type="text"
        autoComplete="off"
        defaultValue={state?.values?.from_name}
      />
      <Field
        label="Reply-to address (optional)"
        name="reply_to_address"
        type="email"
        autoComplete="off"
        defaultValue={state?.values?.reply_to_address}
      />
      <SubmitButton pending={pending}>Connect email</SubmitButton>
    </form>
  );
}
