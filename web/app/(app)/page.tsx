import Link from "next/link";

import { ActivityPanel } from "@/components/ActivityPanel";
import { Markdown } from "@/components/Markdown";
import { NotificationList } from "@/components/NotificationList";
import { Badge, Card, PageTitle } from "@/components/ui";
import { todayIn } from "@/lib/form";
import { backend, requireMe, timeZone } from "@/lib/server";
import type { Activity, Notification, ReportView, TeamSnapshot } from "@/lib/types";

const STATUS: Record<string, { label: string; tone: "amber" | "green" | "indigo" }> = {
  draft: { label: "草稿", tone: "amber" },
  confirmed: { label: "已提交", tone: "green" },
  sent: { label: "已同步", tone: "indigo" },
};

export default async function Home() {
  const me = await requireMe();
  const today = todayIn(timeZone());
  const isManager = me.direct_reports.length > 0;
  const [report, activity, team, notes] = await Promise.all([
    backend<ReportView>(`/reports/${encodeURIComponent(me.user.id)}/${today}`),
    backend<Activity>(`/me/activity?date=${today}`),
    isManager ? backend<TeamSnapshot>(`/team?date=${today}`) : Promise.resolve(null),
    backend<{ unread: number; items: Notification[] }>("/me/notifications"),
  ]);
  const r = report.status === 200 ? report.data : null;
  const snap = team?.status === 200 ? team.data : null;
  const submitted = snap?.members.filter((m) => m.report) ?? [];
  const missing = snap?.members.filter((m) => !m.report) ?? [];

  return (
    <>
      <PageTitle title={`${me.user.name},你好`} subtitle={`${today}${me.manager ? ` · 上级:${me.manager.name}` : ""}`} />
      <div className="grid gap-6 lg:grid-cols-3">
        <Card
          className="lg:col-span-2"
          title="今天的日报"
          actions={
            r ? <Badge tone={STATUS[r.status]?.tone ?? "amber"}>{STATUS[r.status]?.label ?? r.status} · v{r.version}</Badge> : null
          }
        >
          {r ? (
            <>
              <Markdown>{r.rendered_md}</Markdown>
              {r.status === "draft" && (
                <Link href="/generate" className="mt-4 inline-block text-sm text-indigo-600 hover:underline">
                  继续编辑并提交 →
                </Link>
              )}
            </>
          ) : (
            <div className="py-6 text-center">
              <p className="text-sm text-slate-500">今天还没有写日报。</p>
              <Link href="/generate" className="mt-3 inline-block rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500">
                写日报
              </Link>
            </div>
          )}
        </Card>
        <div className="space-y-6">
          <div id="notifications">
            <Card title="通知">
              <NotificationList items={(notes.data?.items ?? []).slice(0, 8)} unread={notes.data?.unread ?? 0} />
            </Card>
          </div>
          <Card title="今天的活动">
            <ActivityPanel activity={activity.status === 200 ? activity.data : null} />
          </Card>
          {snap && (
            <Card title="团队" actions={<Link href="/team" className="text-sm text-indigo-600 hover:underline">打开 →</Link>}>
              <p className="text-sm text-slate-600">
                已提交 <span className="font-semibold">{submitted.length}</span> / {snap.members.length}
              </p>
              {missing.length > 0 && <p className="mt-2 text-sm text-slate-500">未提交:{missing.map((m) => m.user.name).join("、")}</p>}
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
