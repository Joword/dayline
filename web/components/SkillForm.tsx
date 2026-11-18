"use client";

import { type FormEvent, useMemo, useState } from "react";

import { api } from "@/lib/client";
import { collectInputs, type Field, fieldsFor, type FormValues } from "@/lib/form";
import type { SkillInfo, SkillRunResult } from "@/lib/types";

import { RunResult } from "./RunResult";
import { Alert, Button, Card, inputCls, Label } from "./ui";

function Widget({ f, value, onChange }: { f: Field; value: string | boolean; onChange: (v: string | boolean) => void }) {
  const str = typeof value === "string" ? value : "";
  switch (f.widget) {
    case "textarea":
    case "json":
      return (
        <textarea
          className={`${inputCls} min-h-28 ${f.widget === "json" ? "font-mono" : ""}`}
          value={str}
          maxLength={f.maxLength}
          required={f.required}
          onChange={(e) => onChange(e.target.value)}
        />
      );
    case "select":
      return (
        <select className={inputCls} value={str} onChange={(e) => onChange(e.target.value)}>
          {!f.required && <option value="">—</option>}
          {f.choices.map((c) => (
            <option key={c}>{c}</option>
          ))}
        </select>
      );
    case "checkbox":
      return <input type="checkbox" className="h-4 w-4" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />;
    case "date":
      return <input type="date" className={inputCls} value={str} onChange={(e) => onChange(e.target.value)} />;
    case "date_range": {
      const [a = "", b = ""] = str.split("..");
      return (
        <div className="flex items-center gap-2">
          <input type="date" className={inputCls} value={a} onChange={(e) => onChange(`${e.target.value}..${b}`)} />
          <span className="text-slate-400">—</span>
          <input type="date" className={inputCls} value={b} onChange={(e) => onChange(`${a}..${e.target.value}`)} />
        </div>
      );
    }
    case "number":
      return <input type="number" step={1} className={inputCls} value={str} onChange={(e) => onChange(e.target.value)} />;
    default:
      return <input className={inputCls} value={str} maxLength={f.maxLength} onChange={(e) => onChange(e.target.value)} />;
  }
}

/** A skill's form, generated from its manifest inputs (plan §7.5). */
export function SkillForm({ skill, today }: { skill: SkillInfo; today: string }) {
  const fields = useMemo(() => fieldsFor(skill.inputs, today), [skill, today]);
  const [values, setValues] = useState<FormValues>(() => Object.fromEntries(fields.map((f) => [f.name, f.initial])));
  const [lang, setLang] = useState(skill.langs.includes("zh") ? "zh" : skill.langs[0]);
  const [errors, setErrors] = useState<string[]>([]);
  const [result, setResult] = useState<SkillRunResult | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const { inputs, errors: errs } = collectInputs(fields, values);
    setErrors(errs);
    if (errs.length) return;
    setBusy(true);
    setResult(null);
    try {
      setResult(await api<SkillRunResult>(`/skills/${skill.name}/run`, { body: { inputs, lang } }));
    } catch (err) {
      setErrors([(err as Error).message]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card title="输入">
        <form onSubmit={submit} className="space-y-4">
          {fields.map((f) => (
            <label key={f.name} className="block">
              <Label required={f.required} hint={f.help}>
                {f.label}
              </Label>
              <Widget f={f} value={values[f.name]} onChange={(v) => setValues((s) => ({ ...s, [f.name]: v }))} />
            </label>
          ))}
          <label className="block">
            <Label>输出语言</Label>
            <select className={inputCls} value={lang} onChange={(e) => setLang(e.target.value)}>
              <option value="zh">中文</option>
              <option value="en">English</option>
            </select>
          </label>
          {errors.length > 0 && (
            <Alert tone="error">
              {errors.map((x) => (
                <div key={x}>{x}</div>
              ))}
            </Alert>
          )}
          <Button type="submit" loading={busy}>
            运行
          </Button>
        </form>
      </Card>
      <Card title="结果">{result ? <RunResult result={result} /> : <p className="text-sm text-slate-400">运行后在这里显示结果。</p>}</Card>
    </div>
  );
}
