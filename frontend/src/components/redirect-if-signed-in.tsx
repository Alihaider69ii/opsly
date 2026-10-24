import { redirect } from "next/navigation";
import { getCurrentUser } from "@/lib/dal";

// Verifies the session with the backend (not just cookie presence), so a stale cookie simply
// shows the form instead of causing a redirect loop.
export async function RedirectIfSignedIn() {
  if (await getCurrentUser()) redirect("/dashboard");
  return null;
}
