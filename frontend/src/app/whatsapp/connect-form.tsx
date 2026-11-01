"use client";

import { useActionState } from "react";
import { Field, FormError, SubmitButton } from "@/components/form-ui";
import { connectWhatsApp } from "./actions";

export function ConnectForm() {
  const [state, action, pending] = useActionState(connectWhatsApp, null);

  return (
    <form action={action} className="space-y-3">
      {state?.error ? <FormError message={state.error} /> : null}
      <Field
        label="Phone number ID"
        name="phone_number_id"
        type="text"
        autoComplete="off"
        defaultValue={state?.values?.phone_number_id}
        error={state?.fieldErrors?.phone_number_id}
        hint="From your WhatsApp Business Account in Meta Business Manager."
      />
      <Field
        label="WhatsApp Business Account ID"
        name="waba_id"
        type="text"
        autoComplete="off"
        defaultValue={state?.values?.waba_id}
        error={state?.fieldErrors?.waba_id}
      />
      <Field
        label="Access token"
        name="access_token"
        type="password"
        autoComplete="off"
        error={state?.fieldErrors?.access_token}
        hint="A permanent token for a System User with access to this WABA. Never shown again after saving."
      />
      <Field
        label="Default country (optional)"
        name="default_country"
        type="text"
        maxLength={2}
        placeholder="US"
        autoComplete="off"
        defaultValue={state?.values?.default_country}
        hint="Two-letter code (ISO 3166-1), used only to interpret phone numbers entered without a country code."
      />
      <SubmitButton pending={pending}>Connect WhatsApp</SubmitButton>
    </form>
  );
}
