"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

// While a crawl is queued or running, re-render the server components every few seconds so the
// status, page count and discovered documents update without a manual reload.
export function AutoRefresh({
  active,
  intervalMs = 3000,
}: {
  active: boolean;
  intervalMs?: number;
}) {
  const router = useRouter();
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => router.refresh(), intervalMs);
    return () => clearInterval(timer);
  }, [active, intervalMs, router]);
  return null;
}
