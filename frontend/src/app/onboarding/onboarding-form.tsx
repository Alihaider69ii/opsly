"use client";

import { useActionState } from "react";
import { Field, FormError, SelectField, SubmitButton } from "@/components/form-ui";
import { BUSINESS_TYPES } from "@/lib/business-types";
import { createOrganization } from "./actions";

export function OnboardingForm() {
  const [state, action, pending] = useActionState(createOrganization, null);
  return (
    <form action={action} className="space-y-4">
      {state?.error ? <FormError message={state.error} /> : null}
      <Field
        label="Business name"
        name="name"
        required
        maxLength={200}
        autoComplete="organization"
        defaultValue={state?.values?.name}
        error={state?.fieldErrors?.name}
      />
      <SelectField
        label="Business type"
        name="business_type"
        placeholder="Select a type…"
        options={BUSINESS_TYPES}
        defaultValue={state?.values?.business_type}
        error={state?.fieldErrors?.business_type}
      />
      <Field
        label="Website"
        name="website"
        inputMode="url"
        autoComplete="url"
        placeholder="https://yourbrand.com"
        defaultValue={state?.values?.website}
        error={state?.fieldErrors?.website}
        hint="Optional. We will learn about your business from it in a later step."
      />
      <SubmitButton pending={pending}>Create organization</SubmitButton>
    </form>
  );
}
