"use client";

import { useActionState } from "react";
import { Field, FormError, SubmitButton } from "@/components/form-ui";
import { signup } from "../actions";

export function SignupForm() {
  const [state, action, pending] = useActionState(signup, null);
  return (
    <form action={action} className="space-y-4">
      {state?.error ? <FormError message={state.error} /> : null}
      <Field
        label="Your name"
        name="full_name"
        autoComplete="name"
        defaultValue={state?.values?.full_name}
        error={state?.fieldErrors?.full_name}
      />
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
        autoComplete="new-password"
        required
        minLength={10}
        maxLength={128}
        hint="At least 10 characters."
        error={state?.fieldErrors?.password}
      />
      <SubmitButton pending={pending}>Create account</SubmitButton>
    </form>
  );
}
