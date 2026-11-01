import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { PriorityBadge } from "@/components/priority-badge";
import { StatusBadge } from "@/components/status-badge";
import { getCurrentOrganization, getCurrentUser, getOperationalTasks } from "@/lib/dal";
import { formatDateTime } from "@/lib/format";
import { TaskActions } from "./task-actions";

export const metadata = { title: "Tasks · Operations · BizFlow AI" };

type SearchParams = Promise<{ status?: string; priority?: string }>;

const STATUSES = ["All", "OPEN", "ASSIGNED", "IN_PROGRESS", "WAITING", "ESCALATED", "RESOLVED", "CANCELLED"];
const PRIORITIES = ["All", "URGENT", "HIGH", "NORMAL", "LOW"];

export default function TasksPage({ searchParams }: { searchParams: SearchParams }) {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading tasks…</p>}>
      <Tasks searchParams={searchParams} />
    </Suspense>
  );
}

async function Tasks({ searchParams }: { searchParams: SearchParams }) {
  const { status, priority } = await searchParams;
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");

  const params = new URLSearchParams();
  if (status && status !== "All") params.set("status", status);
  if (priority && priority !== "All") params.set("priority", priority);
  const query = params.toString() ? `?${params.toString()}` : "";

  const tasks = await getOperationalTasks(query);

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 space-y-8 p-8">
      <AppHeader current="operations" />
      <div>
        <p className="text-sm text-zinc-500">
          <Link href="/operations" className="hover:underline">
            Operations
          </Link>{" "}
          · Tasks
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">Operational Tasks</h1>
      </div>

      <section aria-label="Filters" className="flex flex-wrap gap-4 text-sm">
        <FilterGroup name="status" current={status ?? "All"} options={STATUSES} base={{ priority }} />
        <FilterGroup name="priority" current={priority ?? "All"} options={PRIORITIES} base={{ status }} />
      </section>

      <ul className="space-y-2" data-testid="task-list">
        {tasks.map((task) => (
          <li key={task.id} data-task={task.id} data-status={task.status} className="space-y-2 rounded-lg border border-zinc-200 p-3 dark:border-zinc-800">
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span className="min-w-0 flex-1">{task.title}</span>
              <span className="flex shrink-0 items-center gap-1.5">
                <StatusBadge status={task.category} />
                <PriorityBadge priority={task.priority} />
                <StatusBadge status={task.status} />
                <span className="text-xs text-zinc-500">{formatDateTime(task.created_at)}</span>
              </span>
            </div>
            <TaskActions task={task} />
          </li>
        ))}
        {tasks.length === 0 ? <li className="text-sm text-zinc-500">No tasks match these filters.</li> : null}
      </ul>
    </main>
  );
}

function FilterGroup({
  name,
  current,
  options,
  base,
}: {
  name: string;
  current: string;
  options: string[];
  base: Record<string, string | undefined>;
}) {
  return (
    <div className="flex items-center gap-1" role="group" aria-label={name}>
      <span className="text-xs uppercase text-zinc-500">{name}:</span>
      {options.map((option) => {
        const params = new URLSearchParams();
        for (const [key, value] of Object.entries(base)) if (value) params.set(key, value);
        if (option !== "All") params.set(name, option);
        const href = `/operations/tasks${params.toString() ? `?${params.toString()}` : ""}`;
        const active = option === current;
        return (
          <Link
            key={option}
            href={href}
            data-filter={`${name}-${option}`}
            className={`rounded-md px-2 py-1 text-xs ${
              active
                ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-900"
            }`}
          >
            {option}
          </Link>
        );
      })}
    </div>
  );
}
