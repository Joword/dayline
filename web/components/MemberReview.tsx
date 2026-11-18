"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/client";
import type { Escalation, ReportView, SkillRunResult } from "@/lib/types";

import { RunResult } from "./RunResult";
import { Alert, Button, Card, inputCls, Label } from "./ui";

export function MemberReview({ member, name, date }: { member: string; name: string; date: string }) {
  const [tone, setTone] = useState("encouraging");
  const [lang, setLang] = useState("zh");
  const [result, setResult] = useState<SkillRunResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<ReportView | null>(null);
  const [raisedMap, setRaisedMap] = useState<Record<number, Escalation>>({});

  useEffect(() => {
    api<ReportView>(`/reports/${encodeURIComponent(member)}/${date}`).then(setReport).catch(() => setReport(null));
  }, [member, date]);

  async function raise(index: number, escalate: boolean) {
    setError(null);
    try {
      const e = await api<Escalation>("/escalations", { body: { member, date, item_index: index, escalate } });
      setRaisedMap((m) => ({ ...m, [index]: e }));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const blockers = (report?.own_items ?? []).map((it, i) => ({ it, i })).filter(({ it }) => it.kind === "blocker");

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ result: SkillRunResult }>("/team/reviews", { body: { member, date, tone, lang } });
      setResult(r.result);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title={`审阅 ${name} · ${date}`}>
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <label className="block">
          <Label>反馈语气</Label>
          <select className={inputCls} value={tone} onChange={(e) => setTone(e.target.value)}>
            <option value="encouraging">鼓励型</option>
            <option value="direct">直接型</option>
          </select>
        </label>
        <label className="block">
          <Label>语言</Label>
          <select className={inputCls} value={lang} onChange={(e) => setLang(e.target.value)}>
            <option value="zh">中文</option>
            <option value="en">English</option>
          </select>
        </label>
        <Button onClick={run} loading={busy}>
          {result ? "重新审阅" : "开始审阅"}
        </Button>
      </div>
      <p className="mb-4 text-xs text-slate-500">评级针对工作进展,不评价个人。成员授权后,会对照其 git / 日历标出不一致之处(只提示,不下结论)。</p>
      {blockers.length > 0 && (
        <div className="mb-4 space-y-2 rounded-lg border border-slate-200 p-4">
          <p className="text-sm font-medium text-slate-700">阻塞项</p>
          {blockers.map(({ it, i }) => (
            <div key={i} className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span>{it.text}</span>
              {raisedMap[i] ? (
                <span className="text-xs text-emerald-700">{raisedMap[i].status === "escalated" ? `已上报给 ${raisedMap[i].current_holder_id}` : "已在你这一级跟踪"}</span>
              ) : (
                <span className="flex gap-2">
                  <Button variant="secondary" onClick={() => raise(i, true)}>需上级介入</Button>
                  <Button variant="ghost" onClick={() => raise(i, false)}>仅跟踪</Button>
                </span>
              )}
            </div>
          ))}
        </div>
      )}
      {error && <Alert tone="error">{error}</Alert>}
      {result && <RunResult result={result} />}
    </Card>
  );
}
