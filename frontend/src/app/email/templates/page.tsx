import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { StatusBadge } from "@/components/status-badge";
import { getCurrentOrganization, getEmailTemplates, requireUser } from "@/lib/dal";
import { TemplateForm } from "./template-form";

export const metadata = { title: "Email templates · BizFlow AI" };

export default function TemplatesPage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading templates…</p>}>
      <Templates />
    </Suspense>
  );
}

async function Templates() {
  await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");
  const canManage = org.role === "OWNER" || org.role === "ADMIN";
  const templates = await getEmailTemplates();

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 space-y-8 p-8">
      <AppHeader current="email" />
      <div>
        <p className="text-sm text-zinc-500">
          <Link href="/email" className="hover:underline">
            Email
          </Link>{" "}
          · Templates
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">Email templates</h1>
      </div>

      <section aria-label="Templates" className="space-y-2">
        <ul className="space-y-2" data-testid="template-list">
          {templates.map((template) => (
            <li
              key={template.id}
              data-template={template.name}
              className="flex items-center justify-between gap-2 rounded-lg border border-zinc-200 p-3 text-sm dark:border-zinc-800"
            >
              <Link href={`/email/templates/${template.id}`} className="hover:underline">
                {template.name}
              </Link>
              <span className="flex items-center gap-2">
                <span className="text-zinc-500">{template.type}</span>
                <StatusBadge status={template.status} />
              </span>
            </li>
          ))}
          {templates.length === 0 ? <li className="text-sm text-zinc-500">No templates yet.</li> : null}
        </ul>
      </section>

      {canManage ? (
        <section aria-label="New template" className="space-y-3 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
          <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">New template</h2>
          <TemplateForm />
        </section>
      ) : null}
    </main>
  );
}
