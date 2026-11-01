import "server-only";
import { cookies } from "next/headers";

export const SESSION_COOKIE = "bizflow_session";

// The backend access token lives only in this HttpOnly cookie, set and read on the server.
// Client-side JavaScript (including any XSS payload) cannot read it. SameSite=Lax plus
// Server Actions' built-in Origin check covers CSRF.
export async function setSession(token: string, maxAgeSeconds: number): Promise<void> {
  (await cookies()).set(SESSION_COOKIE, token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: maxAgeSeconds,
  });
}

export async function getToken(): Promise<string | undefined> {
  return (await cookies()).get(SESSION_COOKIE)?.value;
}

export async function clearSession(): Promise<void> {
  (await cookies()).delete(SESSION_COOKIE);
}

// View-as-brand (platform admin only -- see docs/platform-admin.md). Holding the viewed
// organization id in its own HttpOnly cookie, rather than trusting anything from the browser's
// JS, means every DAL call can attach it automatically and the backend re-verifies
// is_platform_admin on every request; this cookie alone grants nothing on its own.
export const ADMIN_VIEW_ORG_COOKIE = "bizflow_admin_view_org";

export async function setAdminViewOrg(organizationId: string): Promise<void> {
  (await cookies()).set(ADMIN_VIEW_ORG_COOKIE, organizationId, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: 60 * 60 * 8,
  });
}

export async function getAdminViewOrg(): Promise<string | undefined> {
  return (await cookies()).get(ADMIN_VIEW_ORG_COOKIE)?.value;
}

export async function clearAdminViewOrg(): Promise<void> {
  (await cookies()).delete(ADMIN_VIEW_ORG_COOKIE);
}
