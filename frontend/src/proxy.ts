import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// Must match SESSION_COOKIE in lib/session.ts (that module is server-only and can't be imported here).
const SESSION_COOKIE = "bizflow_session";

// Optimistic guard only: a request with no session cookie can't be logged in, so send it
// straight to /login. Cookie presence proves nothing, so real verification happens in the
// Data Access Layer (lib/dal.ts), which has the backend validate the token on every page.
export function proxy(request: NextRequest) {
  if (!request.cookies.has(SESSION_COOKIE)) {
    return NextResponse.redirect(new URL("/login", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: [
    "/dashboard/:path*",
    "/onboarding/:path*",
    "/knowledge/:path*",
    "/website-chat/:path*",
    "/integrations/:path*",
  ],
};
