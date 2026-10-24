import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { StatusBadge } from "@/components/status-badge";
import { getAutomations, getCurrentOrganization, requireUser } from "@/lib/dal";
import type { AutomationWorkflow } from "@/lib/types";
import { archiveAutomation, activateAutomation, pauseAutomation } from "./actions";
import { CreateAutomationForm } from "./create-form";

export const metadata = { title: "Automations · BizFlow AI" };

export default function AutomationsPage() {
  return <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading automations…</p>}><Automations /></Suspense>;
}

async function Automations() {
  await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");
  const workflows = (await getAutomations()) ?? [];
  const canManage = org.role === "OWNER" || org.role === "ADMIN";
  return <main className="mx-auto w-full max-w-4xl flex-1 space-y-8 p-8">
    <AppHeader current="automations" />
    <div><h1 className="text-2xl font-semibold tracking-tight">Automations</h1><p className="mt-1 text-sm text-zinc-500">Build event-triggered workflows for {org.name}. Workflows start as drafts.</p></div>
    {canManage ? <CreateAutomationForm /> : <p className="text-sm text-zinc-500">Only owners and admins can create or change workflows.</p>}
    <section className="space-y-4" aria-label="Workflows"><h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">Workflows</h2>
      {workflows.length === 0 ? <p className="rounded-lg border border-dashed p-6 text-center text-sm text-zinc-500">No workflows yet.</p> : <ul className="space-y-4">{workflows.map((workflow) => <WorkflowCard key={workflow.id} workflow={workflow} canManage={canManage} />)}</ul>}
    </section>
  </main>;
}

function WorkflowCard({ workflow, canManage }: { workflow: AutomationWorkflow; canManage: boolean }) {
  const recent = workflow.recent_executions ?? [];
  const runs = recent.filter((run) => run.status === "FAILED").length;
  return <li className="space-y-3 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><Link href={`/automations/${workflow.id}`} className="font-medium underline-offset-4 hover:underline">{workflow.name}</Link><p className="text-sm text-zinc-500">Trigger: {workflow.definition.trigger.event.replaceAll("_", " ")} · Version {workflow.version}</p></div><StatusBadge status={workflow.status} /></div>
    {workflow.description ? <p className="text-sm text-zinc-600">{workflow.description}</p> : null}
    <p className="text-xs text-zinc-500">{workflow.definition.conditions.length} conditions · {workflow.definition.actions.length} steps · {recent.length} recent runs{runs ? ` · ${runs} failed` : ""}</p>
    <div className="flex flex-wrap gap-3 text-sm"><Link href={`/automations/${workflow.id}`} className="underline">Inspect configuration and history</Link>
      {canManage && workflow.status !== "ARCHIVED" ? workflow.status === "ACTIVE" ? <form action={pauseAutomation}><input type="hidden" name="id" value={workflow.id} /><button className="underline">Pause</button></form> : <form action={activateAutomation}><input type="hidden" name="id" value={workflow.id} /><button className="underline">Activate</button></form> : null}
      {canManage && workflow.status !== "ARCHIVED" ? <form action={archiveAutomation}><input type="hidden" name="id" value={workflow.id} /><button className="text-red-700 underline">Archive</button></form> : null}
    </div>
  </li>;
}
