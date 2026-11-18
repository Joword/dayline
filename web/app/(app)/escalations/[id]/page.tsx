import Link from "next/link";

import { EscalationActions } from "@/components/EscalationActions";
import { Alert, Badge, Card, PageTitle } from "@/components/ui";
import { backend, requireMe } from "@/lib/server";
import { ESC_STATUS as STATUS } from "@/lib/escalations";
import type { Escalation } from "@/lib/types";

const ACTION: Record<string, string> = {
  created: "创建",
  escalated: "上报",
  accepted: "接手",
  resolved: "解决",
  commented: "补充",
};

export default async function EscalationPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const me = await requireMe();
  const r = await backend<Escalation | { detail: string }>(`/escalations/${encodeURIComponent(id)}`);
  if (r.status !== 200 || !r.data || !("chain" in r.data)) {
    return (
      <>
        <PageTitle title="升级事项" />
        <Alert tone={r.status === 403 ? "error" : "info"}>{r.status === 403 ? "你不在这个升级事项的流转链中。" : "没有找到这个升级事项。"}</Alert>
      </>
    );
  }
  const e = r.data;
  return (
    <>
      <PageTitle title={e.text} subtitle={`来自 ${e.origin.user_id} 的 ${e.origin.date} 日报`} />
      <Link href="/escalations" className="mb-4 inline-block text-sm text-indigo-600 hover:underline">
        ← 全部升级事项
      </Link>
      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2" title="流转记录" actions={<Badge tone={STATUS[e.status]?.tone ?? "slate"}>{STATUS[e.status]?.label}</Badge>}>
          <ol className="relative space-y-4 border-l border-slate-200 pl-5">
            {e.chain.map((ev, i) => (
              <li key={i} className="text-sm">
                <span className="absolute -left-1.5 mt-1.5 h-3 w-3 rounded-full border-2 border-white bg-indigo-400" />
                <div className="text-slate-800">
                  <span className="font-medium">{ev.actor_id}</span> {ACTION[ev.action] ?? ev.action}
                  {ev.action === "escalated" && ev.to_holder && <span className="text-slate-500"> → {ev.to_holder}</span>}
                  <span className="ml-2 text-xs text-slate-400">{new Date(ev.at).toLocaleString("zh-CN")}</span>
                </div>
                {ev.note && <p className="mt-0.5 text-slate-600">{ev.note}</p>}
              </li>
            ))}
          </ol>
          {e.resolution && (
            <div className="mt-4">
              <Alert tone="ok">解决方式:{e.resolution}</Alert>
            </div>
          )}
        </Card>
        <div className="space-y-6">
          <Card title="信息">
            <dl className="space-y-1.5 text-sm">
              <div className="flex justify-between"><dt className="text-slate-500">当前处理人</dt><dd>{e.current_holder_id}</dd></div>
              {e.need_from && <div className="flex justify-between"><dt className="text-slate-500">需要</dt><dd>{e.need_from}</dd></div>}
              {e.severity && <div className="flex justify-between"><dt className="text-slate-500">严重度</dt><dd>{e.severity}</dd></div>}
              <div className="flex justify-between"><dt className="text-slate-500">参与人</dt><dd>{e.participants.join("、")}</dd></div>
            </dl>
          </Card>
          {e.status !== "resolved" && (
            <Card title="操作">
              <EscalationActions esc={e} me={me.user.id} hasManager={Boolean(me.manager)} />
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
