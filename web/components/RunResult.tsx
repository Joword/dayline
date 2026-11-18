import type { SkillRunResult } from "@/lib/types";

import { Markdown } from "./Markdown";
import { Alert } from "./ui";

export function RunResult({ result }: { result: SkillRunResult }) {
  return (
    <div className="space-y-3">
      {result.status !== "ok" && (
        <Alert tone="error">
          <p className="font-medium">输出没有通过校验</p>
          <ul className="mt-1 list-disc pl-5">
            {result.errors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </Alert>
      )}
      {result.notice && <Alert tone="warn">{result.notice}</Alert>}
      {result.warnings.map((w) => (
        <Alert key={w} tone="warn">
          {w}
        </Alert>
      ))}
      {result.markdown && <Markdown>{result.markdown}</Markdown>}
      <p className="text-xs text-slate-400">
        {result.endpoint} · {result.model} · tokens {result.usage.input_tokens}/{result.usage.output_tokens}
        {result.cost_usd != null && ` · $${result.cost_usd.toFixed(4)}`} · {(result.latency_ms / 1000).toFixed(1)}s
      </p>
    </div>
  );
}
