import Link from "next/link";
import { adminLogout } from "@/app/admin/actions";

const links = [
  { href: "/admin", label: "Overview", key: "overview" },
  { href: "/admin/brands", label: "Brands", key: "brands" },
  { href: "/admin/audit", label: "Activity", key: "activity" },
] as const;

export function AdminHeader({ current }: { current: (typeof links)[number]["key"] }) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <p className="font-semibold tracking-tight">BizFlow AI · Platform Admin</p>
        <nav aria-label="Platform admin" className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
          {links.map((link) => (
            <Link
              key={link.key}
              href={link.href}
              aria-current={link.key === current ? "page" : undefined}
              className={
                link.key === current
                  ? "font-medium underline underline-offset-4"
                  : "text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100"
              }
            >
              {link.label}
            </Link>
          ))}
        </nav>
      </div>
      <form action={adminLogout}>
        <button className="text-sm text-zinc-500 underline hover:text-zinc-900 dark:hover:text-zinc-100">
          Log out
        </button>
      </form>
    </header>
  );
}
