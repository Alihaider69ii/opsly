import { Suspense } from "react";
import Link from "next/link";
import { redirect } from "next/navigation";
import { AppHeader } from "@/components/app-header";
import { getCurrentOrganization, getCurrentUser } from "@/lib/dal";
import { AIChat } from "./ai-chat";

export const metadata = { title: "Ask Operations AI · BizFlow AI" };

export default function OperationsAIPage() {
  return (
    <Suspense fallback={<p className="p-8 text-sm text-zinc-500">Loading…</p>}>
      <OperationsAI />
    </Suspense>
  );
}

async function OperationsAI() {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  const org = await getCurrentOrganization();
  if (!org) redirect("/onboarding");

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 space-y-8 p-8">
      <AppHeader current="operations" />
      <div>
        <p className="text-sm text-zinc-500">
          <Link href="/operations" className="hover:underline">
            Operations
          </Link>{" "}
          · Ask AI
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">Ask Operations AI</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Answers come only from this organization&apos;s current operational data. When nothing
          matches, it says so instead of guessing.
        </p>
      </div>

      <AIChat />
    </main>
  );
}
