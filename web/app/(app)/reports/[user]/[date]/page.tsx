import { Markdown } from "@/components/Markdown";
import { Alert, Badge, Card, PageTitle } from "@/components/ui";
import { backend } from "@/lib/server";
import type { ReportView } from "@/lib/types";

const SYNC: Record<string, { label: string; tone: "amber" | "green" | "red" }> = {
  pending: { label: "钉钉同步中", tone: "amber" },
  synced: { label: "已同步钉钉", tone: "green" },
  failed: { label: "钉钉同步失败", tone: "red" },
};

const ACCESS: Record<string, string> = {
  full: "完整",
  dependencies: "仅跨团队依赖条目",
};

export default async function ReportPage({ params }: { params: Promise<{ user: string; date: string }> }) {
  const { user, date } = await params;
  const r = await backend<ReportView | { detail: string }>(`/reports/${encodeURIComponent(user)}/${encodeURIComponent(date)}`);
  if (r.status !== 200 || !r.data || !("id" in r.data)) {
    const detail = r.data && "detail" in r.data ? r.data.detail : "";
    return (
      <>
        <PageTitle title={`${user} · ${date}`} />
        <Alert tone={r.status === 403 ? "error" : "info"}>
          {r.status === 403 ? `无权查看:${detail}` : r.status === 404 ? "这一天没有已提交的日报。" : `出错了:${detail}`}
        </Alert>
      </>
    );
  }
  const v = r.data;
  return (
    <>
      <PageTitle title={`${v.user.name} · ${v.date}`} subtitle={v.user.org_unit ?? undefined} />
      <Card
        actions={
          <div className="flex gap-2">
            <Badge>v{v.version}</Badge>
            <Badge tone={v.status === "draft" ? "amber" : "green"}>{v.status}</Badge>
            <Badge tone={v.access === "full" ? "indigo" : "amber"}>{ACCESS[v.access] ?? v.access}</Badge>
            {SYNC[v.sync_status] && <Badge tone={SYNC[v.sync_status].tone}>{SYNC[v.sync_status].label}</Badge>}
          </div>
        }
      >
        <Markdown>{v.rendered_md || "(空)"}</Markdown>
      </Card>
    </>
  );
}
