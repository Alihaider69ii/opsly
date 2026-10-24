import Link from "next/link";
import { logout } from "@/app/(auth)/actions";

const links = [
  { href: "/dashboard", label: "Dashboard", key: "dashboard" },
  { href: "/knowledge", label: "Knowledge Base", key: "knowledge" },
  { href: "/website-chat", label: "Website Chat", key: "website-chat" },
  { href: "/integrations/shopify", label: "Shopify", key: "shopify" },
] as const;

export function AppHeader({ current }: { current: (typeof links)[number]["key"] }) {
  return (
    <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <p className="font-semibold tracking-tight">BizFlow AI</p>
        <nav aria-label="Main" className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
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
      <form action={logout}>
        <button className="text-sm text-zinc-500 underline hover:text-zinc-900 dark:hover:text-zinc-100">
          Log out
        </button>
      </form>
    </header>
  );
}
