"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { api } from "@/lib/client";
import type { SkillRunResult, StoredReport } from "@/lib/types";

import { RunResult } from "./RunResult";
import { Alert, Button, Card, inputCls, Label } from "./ui";

interface GenerateResponse {
  result: SkillRunResult;
  report: StoredReport | null;
}

export function GenerateFlow({ today }: { today: string }) {
  const router = useRouter();
  const [narrative, setNarrative] = useState("");
  const [date, setDate] = useState(today);
  const [lang, setLang] = useState("zh");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [answered, setAnswered] = useState<string[]>([]);
  const [resp, setResp] = useState<GenerateResponse | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState<"gen" | "confirm" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const questions =
    resp?.result.status === "ok" && resp.result.output && typeof resp.result.output === "object"
      ? ((resp.result.output as { questions?: string[] }).questions ?? [])
      : [];

  async function generate(withAnswers: string[]) {
    setBusy("gen");
    setError(null);
    setConfirmed(false);
    try {
      const r = await api<GenerateResponse>("/reports/generate", {
        body: { narrative, date, lang, answers: withAnswers.length ? withAnswers.join("\n\n") : null },
      });
      setResp(r);
      setAnswers({});
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  function regenerateWithAnswers() {
    const fresh = questions.filter((q) => answers[q]?.trim()).map((q) => `Q: ${q}\nA: ${answers[q].trim()}`);
    const all = [...answered, ...fresh];
    setAnswered(all);
    generate(all);
  }

  async function confirm() {
    if (!resp?.report) return;
    setBusy("confirm");
    setError(null);
    try {
      await api(`/reports/${resp.report.id}/confirm`, { method: "POST", body: {} });
      setConfirmed(true);
      router.refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card title="今天做了什么">
        <div className="space-y-4">
          <label className="block">
            <Label required hint="用自己的话写,越具体越好;git 和日历会自动带上">
              自述
            </Label>
            <textarea
              className={`${inputCls} min-h-48`}
              value={narrative}
              onChange={(e) => setNarrative(e.target.value)}
              placeholder="例如:上午把支付回调的重试上线了,失败率从 2% 降到 0.3%。下午写对账报表,完成六成,周三能写完。风控的接口文档还没给……"
            />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <Label>日期</Label>
              <input type="date" className={inputCls} value={date} onChange={(e) => setDate(e.target.value)} />
            </label>
            <label className="block">
              <Label>语言</Label>
              <select className={inputCls} value={lang} onChange={(e) => setLang(e.target.value)}>
                <option value="zh">中文</option>
                <option value="en">English</option>
              </select>
            </label>
          </div>
          <Button onClick={() => { setAnswered([]); generate([]); }} loading={busy === "gen"} disabled={!narrative.trim()}>
            {resp ? "重新生成" : "生成日报"}
          </Button>
          {questions.length > 0 && (
            <div className="space-y-3 rounded-lg border border-indigo-100 bg-indigo-50/50 p-4">
              <p className="text-sm font-medium text-indigo-900">还有几处信息需要补充(可跳过):</p>
              {questions.map((q) => (
                <label key={q} className="block">
                  <span className="mb-1 block text-sm text-slate-700">{q}</span>
                  <input className={inputCls} value={answers[q] ?? ""} onChange={(e) => setAnswers((a) => ({ ...a, [q]: e.target.value }))} />
                </label>
              ))}
              <Button variant="secondary" onClick={regenerateWithAnswers} loading={busy === "gen"}>
                补充后重新生成
              </Button>
            </div>
          )}
          {error && <Alert tone="error">{error}</Alert>}
        </div>
      </Card>
      <Card
        title="日报预览"
        actions={
          resp?.report && !confirmed ? (
            <Button onClick={confirm} loading={busy === "confirm"}>
              确认提交
            </Button>
          ) : null
        }
      >
        {confirmed && <div className="mb-3"><Alert tone="ok">已提交,直属上级现在可以看到这份日报。</Alert></div>}
        {resp ? (
          <>
            {resp.report && <p className="mb-2 text-xs text-slate-400">已保存草稿 v{resp.report.version}</p>}
            <RunResult result={resp.result} />
          </>
        ) : (
          <p className="text-sm text-slate-400">生成后在这里预览,确认后才会提交给上级。</p>
        )}
      </Card>
    </div>
  );
}
