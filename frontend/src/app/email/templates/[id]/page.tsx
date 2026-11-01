import { Suspense } from "react";
import Link from "next/link";
import { notFound, redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { StatusBadge } from "@/components/status-badge";
import { getCurrentOrganization, getEmailTemplate, requireUser } from "@/lib/dal";
import { isUuid } from "@/lib/format";
import { TemplateForm } from "../template-form";

export const metadata = { title: "Edit template · BizFlow AI" };

export default function TemplatePage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading template…</p>}>
      <Template params={params} />
    </Suspense>
  );
}

async function Template({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!isUuid(id)) notFound();
  await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");
  const template = await getEmailTemplate(id);
  if (!template) notFound();

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 space-y-8 p-8">
      <AppHeader current="email" />
      <div>
        <p className="text-sm text-zinc-500">
          <Link href="/email/templates" className="hover:underline">
            Templates
          </Link>{" "}
          · {template.name}
        </p>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          {template.name}
          <StatusBadge status={template.status} />
        </h1>
      </div>
      <TemplateForm existing={template} />
    </main>
  );
}
