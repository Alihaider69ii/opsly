import { Suspense } from "react";
import { notFound, redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { getCurrentOrganization, requireUser } from "@/lib/dal";
import { getToken } from "@/lib/session";
import { apiFetch } from "@/lib/api";
import type { AutomationExecution, AutomationWorkflow } from "@/lib/types";

type WorkflowDetail = AutomationWorkflow & { recent_executions: AutomationExecution[] };

export default function AutomationDetailPage({ params }: { params: Promise<{ id: string }> }) {
  return <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading workflow…</p>}><AutomationDetail params={params} /></Suspense>;
}

async function AutomationDetail({ params }: { params: Promise<{ id: string }> }) {
  await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");
  const { id } = await params;
  if (!/^[0-9a-f-]{36}$/i.test(id)) notFound();
  const token = await getToken();
  if (!token) redirect("/login");
  let workflow: WorkflowDetail;
  try { workflow = await apiFetch<WorkflowDetail>(`/api/automations/${id}`, { token }); } catch { notFound(); }
  if (!workflow) notFound();
  const runs = workflow.recent_executions;
  return <main className="mx-auto w-full max-w-4xl flex-1 space-y-8 p-8"><AppHeader current="automations" />
    <div><p className="text-sm text-zinc-500">Workflow · {workflow.status} · Version {workflow.version}</p><h1 className="text-2xl font-semibold">{workflow.name}</h1>{workflow.description ? <p className="mt-1 text-sm text-zinc-500">{workflow.description}</p> : null}</div>
    <section className="space-y-3"><h2 className="font-medium">Trigger and conditions</h2><pre className="overflow-auto rounded-lg bg-zinc-100 p-4 text-xs dark:bg-zinc-900">{JSON.stringify({ trigger: workflow.definition.trigger, conditions: workflow.definition.conditions }, null, 2)}</pre></section>
    <section className="space-y-3"><h2 className="font-medium">Steps</h2><ol className="space-y-2">{workflow.definition.actions.map((action, index) => <li key={index} className="rounded-md border p-3 text-sm">{index + 1}. {String(action.type).replaceAll("_", " ")} <code className="ml-2 text-xs text-zinc-500">{JSON.stringify(action)}</code></li>)}</ol></section>
    <section className="space-y-3"><h2 className="font-medium">Recent executions</h2>{runs.length ? <ul className="divide-y rounded-lg border">{runs.map((run) => <li key={run.id} className="flex flex-wrap items-center justify-between gap-2 p-3 text-sm"><a href={`/automations/executions/${run.id}`} className="underline">{run.id}</a><span>{run.status}{run.error_code ? ` · ${run.error_code}` : ""} · attempt {run.attempts}</span><time>{new Date(run.created_at).toLocaleString()}</time></li>)}</ul> : <p className="text-sm text-zinc-500">No executions yet.</p>}</section>
  </main>;
}
