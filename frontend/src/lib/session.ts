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
