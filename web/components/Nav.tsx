"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { cn } from "./ui";

export function Nav({ name, isManager, unread = 0, held = 0 }: { name: string; isManager: boolean; unread?: number; held?: number }) {
  const path = usePathname();
  const router = useRouter();
  const links = [
    { href: "/", label: "今日" },
    { href: "/generate", label: "写日报" },
    { href: "/review", label: "审阅日报" },
    ...(isManager ? [{ href: "/team", label: "团队" }] : []),
    { href: "/escalations", label: "升级事项", count: held },
    { href: "/skills", label: "Skills" },
    { href: "/settings", label: "设置" },
  ];

  async function logout() {
    await fetch("/api/session", { method: "DELETE" });
    router.replace("/login");
    router.refresh();
  }

  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-6 px-4">
        <Link href="/" className="font-semibold text-slate-900">
          Dayline
        </Link>
        <nav className="flex flex-1 gap-1 overflow-x-auto">
          {links.map((l) => {
            const active = l.href === "/" ? path === "/" : path.startsWith(l.href);
            return (
              <Link
                key={l.href}
                href={l.href}
                className={cn(
                  "whitespace-nowrap rounded-md px-3 py-1.5 text-sm",
                  active ? "bg-indigo-50 font-medium text-indigo-700" : "text-slate-600 hover:bg-slate-100",
                )}
              >
                {l.label}
                {"count" in l && l.count ? <span className="ml-1.5 rounded-full bg-amber-500 px-1.5 text-xs text-white">{l.count}</span> : null}
              </Link>
            );
          })}
        </nav>
        <div className="flex items-center gap-3 text-sm text-slate-600">
          <Link href="/#notifications" className="relative rounded-md px-2 py-1 hover:bg-slate-100" title="通知">
            通知
            {unread > 0 && <span className="ml-1 rounded-full bg-rose-500 px-1.5 text-xs text-white">{unread}</span>}
          </Link>
          <span className="hidden sm:inline">{name}</span>
          <button onClick={logout} className="rounded-md px-2 py-1 text-slate-500 hover:bg-slate-100">
            退出
          </button>
        </div>
      </div>
    </header>
  );
}
