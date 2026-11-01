"use client";

import { useActionState } from "react";
import { Field, FormError, SelectField, SubmitButton } from "@/components/form-ui";
import { createBrand } from "../actions";

const ENVIRONMENTS = [
  { value: "PRODUCTION", label: "Production" },
  { value: "DEMO", label: "Demo" },
] as const;

export function CreateBrandForm() {
  const [state, action, pending] = useActionState(createBrand, null);
  return (
    <form action={action} className="mt-4 grid gap-4 sm:grid-cols-2">
      {state?.error ? (
        <div className="sm:col-span-2">
          <FormError message={state.error} />
        </div>
      ) : null}
      {state?.message ? (
        <p className="sm:col-span-2 rounded-md border border-green-300 bg-green-50 px-3 py-2 text-sm text-green-700 dark:border-green-900 dark:bg-green-950 dark:text-green-300">
          {state.message}
        </p>
      ) : null}
      <Field
        label="Brand Name"
        name="name"
        required
        defaultValue={state?.values?.name}
        error={state?.fieldErrors?.name}
      />
      <Field
        label="Slug (optional)"
        name="slug"
        defaultValue={state?.values?.slug}
        error={state?.fieldErrors?.slug}
      />
      <SelectField
        label="Brand Type"
        name="environment"
        placeholder="Choose one"
        defaultValue="PRODUCTION"
        options={ENVIRONMENTS}
        required
      />
      <Field
        label="Owner Name"
        name="owner_name"
        required
        defaultValue={state?.values?.owner_name}
        error={state?.fieldErrors?.owner_name}
      />
      <Field
        label="Owner Email"
        name="owner_email"
        type="email"
        required
        defaultValue={state?.values?.owner_email}
        error={state?.fieldErrors?.owner_email}
      />
      <Field
        label="Temporary Password"
        name="temporary_password"
        type="password"
        required
        error={state?.fieldErrors?.temporary_password}
      />
      <div className="sm:col-span-2">
        <SubmitButton pending={pending}>Create Brand</SubmitButton>
      </div>
    </form>
  );
}
