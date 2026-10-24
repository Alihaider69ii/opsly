import "server-only";
import { headers } from "next/headers";

// The browser talks to Next.js, and Next.js talks to the backend, so the backend would see
// every user as the Next.js server's IP. Forwarding the original X-Forwarded-For chain
// (unmodified) lets the backend's trusted-proxy handling pick out the real client for rate
// limiting. It is only honored when the backend is configured to trust this server
// (uvicorn FORWARDED_ALLOW_IPS), and this server must itself sit behind a proxy that sets it.
export async function forwardedFor(): Promise<string | undefined> {
  const h = await headers();
  return h.get("x-forwarded-for") ?? h.get("x-real-ip") ?? undefined;
}
