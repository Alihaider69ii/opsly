import { Suspense } from "react";
import Link from "next/link";

// Server-side only: the browser never needs to know the backend URL for this check.
const API_URL = process.env.API_URL ?? "http://localhost:8000";

async function getBackendStatus(): Promise<"ok" | "unreachable"> {
  try {
    const res = await fetch(`${API_URL}/health`, {
      cache: "no-store",
      signal: AbortSignal.timeout(3000),
    });
    return res.ok ? "ok" : "unreachable";
  } catch {
    return "unreachable";
  }
}

async function BackendStatus() {
  const status = await getBackendStatus();
  return <span className={status === "ok" ? "text-green-600" : "text-red-600"}>{status}</span>;
}

export default function Home() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-6 p-8 text-center">
      <h1 className="text-4xl font-semibold tracking-tight">BizFlow AI</h1>
      <p className="text-zinc-600 dark:text-zinc-400">
        The AI automation layer for your online business.
      </p>
      <div className="flex gap-3">
        <Link
          href="/signup"
          className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          Sign up
        </Link>
        <Link
          href="/login"
          className="rounded-md border border-zinc-300 px-4 py-2 text-sm font-medium hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-900"
        >
          Log in
        </Link>
      </div>
      <p className="rounded-full border border-zinc-300 px-4 py-1.5 text-sm dark:border-zinc-700">
        Backend:{" "}
        <Suspense fallback={<span className="text-zinc-500">checking…</span>}>
          <BackendStatus />
        </Suspense>
      </p>
    </main>
  );
}
