import { Suspense } from "react";
import { notFound, redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { getCurrentOrganization, requireUser } from "@/lib/dal";
import { getToken } from "@/lib/session";
import { apiFetch } from "@/lib/api";

export default function AutomationExecutionPage({ params }: { params: Promise<{ id: string }> }) {
  return <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading execution…</p>}><ExecutionDetail params={params} /></Suspense>;
}

async function ExecutionDetail({ params }: { params: Promise<{ id: string }> }) {
  await requireUser();
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");
  const { id } = await params;
  if (!/^[0-9a-f-]{36}$/i.test(id)) notFound();
  const token = await getToken();
  if (!token) redirect("/login");
  let execution: { id: string; status: string; error_code: string | null; workflow_version: number; workflow_snapshot: unknown; steps: Array<{ index: number; action: Record<string, unknown>; status: string; attempts: number; error_code: string | null; result: unknown; scheduled_at: string | null }> };
  try { execution = await apiFetch<typeof execution>(`/api/automations/executions/${id}`, { token }); } catch { notFound(); }
  return <main className="mx-auto w-full max-w-4xl flex-1 space-y-8 p-8"><AppHeader current="automations" /><div><h1 className="text-2xl font-semibold">Execution</h1><p className="text-sm text-zinc-500">{execution.id} · {execution.status} · workflow version {execution.workflow_version}{execution.error_code ? ` · ${execution.error_code}` : ""}</p></div>
    <section className="space-y-3"><h2 className="font-medium">Workflow snapshot</h2><pre className="overflow-auto rounded-lg bg-zinc-100 p-4 text-xs dark:bg-zinc-900">{JSON.stringify(execution.workflow_snapshot, null, 2)}</pre></section>
    <section className="space-y-3"><h2 className="font-medium">Step history</h2>{execution.steps.length ? <ol className="space-y-3">{execution.steps.map((step) => <li key={step.index} className="rounded-lg border p-4"><p className="font-medium">Step {step.index + 1}: {String(step.action.type)} · {step.status}</p><p className="text-sm text-zinc-500">Attempts: {step.attempts}{step.error_code ? ` · ${step.error_code}` : ""}{step.scheduled_at ? ` · scheduled ${step.scheduled_at}` : ""}</p>{step.result ? <pre className="mt-2 overflow-auto text-xs">{JSON.stringify(step.result, null, 2)}</pre> : null}</li>)}</ol> : <p className="text-sm text-zinc-500">No steps have run.</p>}</section>
  </main>;
}
