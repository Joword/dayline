import Link from "next/link";

import { Badge, Card, PageTitle, cn } from "@/components/ui";
import { ESC_STATUS as STATUS } from "@/lib/escalations";
import { backend, requireMe } from "@/lib/server";
import type { Escalation } from "@/lib/types";


const TABS = [
  { scope: "held", label: "等我处理" },
  { scope: "raised", label: "我上报的" },
  { scope: "involved", label: "与我相关" },
];

export default async function EscalationsPage({ searchParams }: { searchParams: Promise<{ scope?: string; all?: string }> }) {
  await requireMe();
  const { scope = "held", all } = await searchParams;
  const r = await backend<Escalation[]>(`/escalations?scope=${encodeURIComponent(scope)}&include_resolved=${all ? "true" : "false"}`);
  const items = r.data ?? [];
  return (
    <>
      <PageTitle title="升级事项" subtitle="下属的阻塞被标记为需上级介入后在这里逐级传递:接手、就地解决或继续上报,全程留痕。" />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {TABS.map((t) => (
          <Link
            key={t.scope}
            href={`/escalations?scope=${t.scope}${all ? "&all=1" : ""}`}
            className={cn("rounded-md px-3 py-1.5 text-sm", scope === t.scope ? "bg-indigo-50 font-medium text-indigo-700" : "text-slate-600 hover:bg-slate-100")}
          >
            {t.label}
          </Link>
        ))}
        <Link href={`/escalations?scope=${scope}${all ? "" : "&all=1"}`} className="ml-auto text-sm text-slate-500 hover:underline">
          {all ? "隐藏已解决" : "显示已解决"}
        </Link>
      </div>
      <Card>
        {items.length === 0 ? (
          <p className="text-sm text-slate-400">没有升级事项。</p>
        ) : (
          <ul className="divide-y divide-slate-100">
            {items.map((e) => (
              <li key={e.id} className="py-3">
                <Link href={`/escalations/${e.id}`} className="group flex items-start justify-between gap-4">
                  <div>
                    <p className="text-sm font-medium text-slate-800 group-hover:text-indigo-700">{e.text}</p>
                    <p className="mt-0.5 text-xs text-slate-500">
                      来自 {e.origin.user_id} · {e.origin.date} · 当前处理人 {e.current_holder_id}
                      {e.need_from && ` · 需要 ${e.need_from}`}
                    </p>
                  </div>
                  <Badge tone={STATUS[e.status]?.tone ?? "slate"}>{STATUS[e.status]?.label ?? e.status}</Badge>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}
