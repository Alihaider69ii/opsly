import { exitBrand } from "@/app/admin/actions";
import { getAdminViewOrg } from "@/lib/session";
import { getCurrentOrganization, getCurrentUser } from "@/lib/dal";

// Brief section 12: "Display a highly visible banner" whenever a platform admin is viewing a
// brand's workspace. Lives at the root layout so it renders over every brand-workspace page
// without each of those pages needing to know about view-as-brand at all.
export async function AdminViewBanner() {
  const viewOrgId = await getAdminViewOrg();
  if (!viewOrgId) return null;
  const user = await getCurrentUser();
  if (!user?.is_platform_admin) return null;
  const org = await getCurrentOrganization();
  if (!org) return null;

  return (
    <div className="flex items-center justify-between gap-4 bg-amber-500 px-4 py-2 text-sm font-medium text-amber-950">
      <span>
        ⚠ PLATFORM ADMIN VIEW — Viewing: <strong>{org.name}</strong>
        {org.environment === "DEMO" ? " (DEMO)" : null}
      </span>
      <form action={exitBrand}>
        <input type="hidden" name="organization_id" value={org.id} />
        <button type="submit" className="rounded bg-amber-950 px-3 py-1 text-amber-50 hover:bg-amber-900">
          Exit Brand View
        </button>
      </form>
    </div>
  );
}
