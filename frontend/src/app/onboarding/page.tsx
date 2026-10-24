import { Suspense } from "react";
import { redirect } from "next/navigation";
import { CenteredCard } from "@/components/centered-card";
import { getOrganizations, requireUser } from "@/lib/dal";
import { OnboardingForm } from "./onboarding-form";

export const metadata = { title: "Set up your business · BizFlow AI" };

export default function OnboardingPage() {
  return (
    <CenteredCard title="Set up your business" subtitle="Tell us a little about your brand">
      <Suspense fallback={<p className="text-center text-sm text-zinc-500">Loading…</p>}>
        <Onboarding />
      </Suspense>
    </CenteredCard>
  );
}

async function Onboarding() {
  await requireUser();
  if ((await getOrganizations()).length > 0) redirect("/dashboard");
  return <OnboardingForm />;
}
