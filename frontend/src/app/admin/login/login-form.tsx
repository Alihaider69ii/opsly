"use client";

import { useActionState } from "react";
import { Field, FormError, SubmitButton } from "@/components/form-ui";
import { adminLogin } from "../actions";

export function LoginForm() {
  const [state, action, pending] = useActionState(adminLogin, null);
  return (
    <form action={action} className="space-y-4">
      {state?.error ? <FormError message={state.error} /> : null}
      <Field
        label="Email"
        name="email"
        type="email"
        autoComplete="email"
        required
        defaultValue={state?.values?.email}
        error={state?.fieldErrors?.email}
      />
      <Field
        label="Password"
        name="password"
        type="password"
        autoComplete="current-password"
        required
        error={state?.fieldErrors?.password}
      />
      <SubmitButton pending={pending}>Sign in</SubmitButton>
    </form>
  );
}
