import { Suspense } from "react";
import { AdminHeader } from "@/components/admin-header";
import { getPlatformAudit, requirePlatformAdmin } from "@/lib/dal";

export const metadata = { title: "Activity · Platform Admin · BizFlow AI" };

export default function AdminAuditPage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading…</p>}>
      <Audit />
    </Suspense>
  );
}

async function Audit() {
  await requirePlatformAdmin();
  const events = await getPlatformAudit();

  return (
    <main className="mx-auto w-full max-w-4xl flex-1 space-y-8 p-8">
      <AdminHeader current="activity" />
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Platform Activity</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Every platform-level action, with the admin&apos;s own identity as the actor (brief section 13).
        </p>
      </div>
      <ul className="space-y-2">
        {events.map((event) => (
          <li
            key={event.id}
            className="rounded-lg border border-zinc-200 p-3 text-sm dark:border-zinc-800"
          >
            <p>
              <span className="font-medium">{event.event_type}</span>{" "}
              <span className="text-zinc-500">
                by {event.actor_email ?? "unknown"}
                {event.organization_name ? ` · ${event.organization_name}` : ""}
              </span>
            </p>
            <p className="text-xs text-zinc-500">{new Date(event.created_at).toLocaleString()}</p>
          </li>
        ))}
        {events.length === 0 ? <li className="text-sm text-zinc-500">No activity yet.</li> : null}
      </ul>
    </main>
  );
}
