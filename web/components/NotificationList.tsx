"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { api } from "@/lib/client";
import type { Notification } from "@/lib/types";

import { Button } from "./ui";

export function NotificationList({ items, unread }: { items: Notification[]; unread: number }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  async function readAll() {
    setBusy(true);
    await api("/me/notifications/read", { body: {} }).catch(() => null);
    setBusy(false);
    router.refresh();
  }

  if (items.length === 0) return <p className="text-sm text-slate-400">暂无通知。</p>;
  return (
    <div>
      <ul className="space-y-2.5">
        {items.map((n) => (
          <li key={n.id} className="flex gap-2 text-sm">
            <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${n.read_at ? "bg-transparent" : "bg-indigo-500"}`} />
            <div>
              {n.link ? (
                <Link href={n.link} className="text-slate-800 hover:text-indigo-700">
                  {n.title}
                </Link>
              ) : (
                <span className="text-slate-800">{n.title}</span>
              )}
              {n.body && <p className="text-xs text-slate-500">{n.body}</p>}
              <p className="text-xs text-slate-400">{new Date(n.created_at).toLocaleString("zh-CN")}</p>
            </div>
          </li>
        ))}
      </ul>
      {unread > 0 && (
        <Button variant="ghost" className="mt-3" onClick={readAll} loading={busy}>
          全部标为已读
        </Button>
      )}
    </div>
  );
}
