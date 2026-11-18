"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/client";
import type { SkillRunResult, StoredReport, TeamSnapshot } from "@/lib/types";

import { RunResult } from "./RunResult";
import { Alert, Badge, Button, Card, inputCls, Label } from "./ui";

interface RollupResponse {
  result: SkillRunResult;
  report: StoredReport | null;
  warnings: string[];
}

export function TeamBoard({ today }: { today: string }) {
  const [date, setDate] = useState(today);
  const [snap, setSnap] = useState<TeamSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rollup, setRollup] = useState<RollupResponse | null>(null);
  const [picked, setPicked] = useState<number[]>([]);
  const [forward, setForward] = useState<string[]>([]);
  const [done, setDone] = useState<{ stale: string[]; escalations: number } | null>(null);
  const [busy, setBusy] = useState<"draft" | "confirm" | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setSnap(await api<TeamSnapshot>(`/team?date=${date}`));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [date]);

  useEffect(() => {
    load();
    setRollup(null);
    setDone(null);
  }, [load]);

  async function draft() {
    setBusy("draft");
    setError(null);
    setDone(null);
    try {
      setRollup(await api<RollupResponse>("/team/rollup", { body: { date } }));
      setPicked([]);
      setForward([]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  async function confirm() {
    if (!rollup?.report) return;
    setBusy("confirm");
    try {
      const r = await api<{ stale_sources: string[] }>(`/team/rollup/${rollup.report.id}/confirm`, { body: { escalate: picked, forward } });
      setDone({ stale: r.stale_sources, escalations: picked.length + forward.length });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const candidates = rollup?.report?.team_summary?.escalation_candidates ?? [];
  const pending = rollup?.report?.team_summary?.pending_escalations ?? [];
  const raised = rollup?.report?.team_summary?.raised_escalations ?? [];
  const submitted = snap?.members.filter((m) => m.report).length ?? 0;

  return (
    <div className="space-y-6">
      <div className="flex items-end gap-3">
        <label className="block">
          <Label>日期</Label>
          <input type="date" className={inputCls} value={date} onChange={(e) => setDate(e.target.value)} />
        </label>
        {snap && (
          <p className="pb-2 text-sm text-slate-500">
            已提交 {submitted} / {snap.members.length}
          </p>
        )}
      </div>
      {error && <Alert tone="error">{error}</Alert>}

      <Card title="成员">
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-slate-500">
            <tr>
              <th className="pb-2 font-medium">成员</th>
              <th className="pb-2 font-medium">状态</th>
              <th className="pb-2 font-medium">条目</th>
              <th className="pb-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {snap?.members.map((m) => (
              <tr key={m.user.id}>
                <td className="py-2.5">
                  {m.user.name} <span className="text-xs text-slate-400">{m.user.id}</span>
                </td>
                <td>{m.report ? <Badge tone="green">已提交 v{m.report.version}</Badge> : <Badge tone="red">未提交</Badge>}</td>
                <td className="text-slate-500">{m.report ? m.report.own_items.length : "—"}</td>
                <td className="space-x-3 text-right">
                  {m.report && (
                    <>
                      <Link className="text-indigo-600 hover:underline" href={`/reports/${encodeURIComponent(m.user.id)}/${date}`}>
                        查看
                      </Link>
                      <Link className="text-indigo-600 hover:underline" href={`/team/review?member=${encodeURIComponent(m.user.id)}&date=${date}`}>
                        审阅
                      </Link>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card
        title="团队汇总"
        actions={
          <Button variant="secondary" onClick={draft} loading={busy === "draft"} disabled={!submitted}>
            {rollup ? "重新生成草稿" : "生成草稿"}
          </Button>
        }
      >
        {!rollup && <p className="text-sm text-slate-400">由系统按组织树取齐下属已提交的日报,AI 起草,你确认后才发送给上级。未提交的人只会列在名单里。</p>}
        {rollup && (
          <div className="space-y-4">
            {rollup.warnings.map((w) => (
              <Alert key={w} tone="warn">
                {w}
              </Alert>
            ))}
            <RunResult result={rollup.result} />
            {rollup.report && !done && (
              <div className="space-y-3 rounded-lg border border-slate-200 p-4">
                {candidates.length > 0 && (
                  <>
                    <p className="text-sm font-medium text-slate-700">选择需要上报给你上级的事项:</p>
                    {candidates.map((c, i) => (
                      <label key={c.text} className="flex items-start gap-2 text-sm">
                        <input
                          type="checkbox"
                          className="mt-0.5 h-4 w-4"
                          checked={picked.includes(i + 1)}
                          onChange={(e) => setPicked((p) => (e.target.checked ? [...p, i + 1] : p.filter((x) => x !== i + 1)))}
                        />
                        <span>
                          {c.text}
                          {c.reason && <span className="text-slate-500"> — {c.reason}</span>}
                        </span>
                      </label>
                    ))}
                  </>
                )}
                {pending.length > 0 && (
                  <>
                    <p className="pt-2 text-sm font-medium text-slate-700">上报给你的事项,需要继续上报的请勾选(其余可在“升级事项”里就地处理):</p>
                    {pending.map((e) => (
                      <label key={e.escalation_id} className="flex items-start gap-2 text-sm">
                        <input
                          type="checkbox"
                          className="mt-0.5 h-4 w-4"
                          checked={forward.includes(e.escalation_id)}
                          onChange={(ev) => setForward((f) => (ev.target.checked ? [...f, e.escalation_id] : f.filter((x) => x !== e.escalation_id)))}
                        />
                        <span>
                          {e.text} <span className="text-slate-500">— 来自 {e.origin}</span>
                        </span>
                      </label>
                    ))}
                  </>
                )}
                {raised.length > 0 && (
                  <p className="pt-2 text-xs text-slate-500">今天已上报的 {raised.length} 项会随汇总一起发送:{raised.map((e) => e.text).join(";")}</p>
                )}
                <Button onClick={confirm} loading={busy === "confirm"}>
                  确认并发送给上级
                </Button>
              </div>
            )}
            {done && (
              <Alert tone="ok">
                已确认,上级现在可以看到团队汇总(上报 {done.escalations} 项)。
                {done.stale.length > 0 && ` 注意:${done.stale.join("、")} 在草稿生成后更新了日报。`}
              </Alert>
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
