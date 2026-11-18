"use client";

import { useState } from "react";

import { RunResult } from "@/components/RunResult";
import { Alert, Button, Card, inputCls, Label, PageTitle } from "@/components/ui";
import { api } from "@/lib/client";
import type { SkillRunResult } from "@/lib/types";

export default function ReviewPage() {
  const [report, setReport] = useState("");
  const [author, setAuthor] = useState("");
  const [context, setContext] = useState("");
  const [lang, setLang] = useState("zh");
  const [result, setResult] = useState<SkillRunResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const inputs: Record<string, string> = { report };
      if (author.trim()) inputs.author = author.trim();
      if (context.trim()) inputs.context = context.trim();
      setResult(await api<SkillRunResult>("/skills/review-member/run", { body: { inputs, lang } }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageTitle title="审阅日报" subtitle="粘贴别人发来的日报,得到摘要、质量问题、追问清单和反馈草稿。" />
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="日报原文">
          <div className="space-y-4">
            <textarea className={`${inputCls} min-h-56`} value={report} onChange={(e) => setReport(e.target.value)} placeholder="粘贴日报文本……" />
            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <Label hint="用于反馈草稿">作者称呼</Label>
                <input className={inputCls} value={author} onChange={(e) => setAuthor(e.target.value)} />
              </label>
              <label className="block">
                <Label>语言</Label>
                <select className={inputCls} value={lang} onChange={(e) => setLang(e.target.value)}>
                  <option value="zh">中文</option>
                  <option value="en">English</option>
                </select>
              </label>
            </div>
            <label className="block">
              <Label hint="可选:对方岗位、负责的项目">背景</Label>
              <input className={inputCls} value={context} onChange={(e) => setContext(e.target.value)} />
            </label>
            {error && <Alert tone="error">{error}</Alert>}
            <Button onClick={run} loading={busy} disabled={!report.trim()}>
              审阅
            </Button>
          </div>
        </Card>
        <Card title="审阅结果">{result ? <RunResult result={result} /> : <p className="text-sm text-slate-400">审阅结果会显示在这里。</p>}</Card>
      </div>
    </>
  );
}
