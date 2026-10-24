import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { ApiError, apiFetch } from "@/lib/api";
import { getToken } from "@/lib/session";
import { callbackRedirectPath, declinedInShopify } from "@/lib/shopify";

// Shopify sends the merchant here after they approve (or decline) the app. This route runs on the
// Next.js server so the request to our API can carry the merchant's session: the API then knows
// which user and organization is completing the flow, and checks that against the one-time `state`
// it issued. The query string is forwarded byte for byte, because Shopify's signature covers it.
// Nothing from the query is ever shown back: the page only displays text for a fixed set of codes.
export async function GET(request: NextRequest) {
  const to = (path: string) => NextResponse.redirect(new URL(path, request.url));

  const token = await getToken();
  if (!token) return to("/login");

  const search = request.nextUrl.search; // includes the leading "?"
  if (declinedInShopify(search))
    return to(callbackRedirectPath({ ok: false, code: "access_denied" }));

  try {
    await apiFetch(`/api/integrations/shopify/callback${search}`, { token });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return to("/login");
    return to(
      callbackRedirectPath({
        ok: false,
        code: error instanceof ApiError ? error.code : undefined,
      }),
    );
  }
  return to(callbackRedirectPath({ ok: true }));
}
