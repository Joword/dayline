import type { Activity } from "@/lib/types";

import { Badge } from "./ui";

export function ActivityPanel({ activity }: { activity: Activity | null }) {
  if (!activity) return <p className="text-sm text-slate-400">没有连接 git 或日历。可在设置页连接。</p>;
  const { git, calendar } = activity;
  const notes = { ...activity.unavailable, ...activity.errors };
  return (
    <div className="space-y-4 text-sm">
      {git && (
        <div>
          <div className="mb-1.5 font-medium text-slate-700">
            git · {git.totals.commits} 个提交 · {git.totals.prs} 个 PR/MR
          </div>
          <ul className="space-y-1">
            {git.repos.map((r) => (
              <li key={r.repo}>
                <span className="font-mono text-xs text-slate-500">{r.repo}</span>
                {r.prs.map((p) => (
                  <a key={p.number} href={p.url} target="_blank" rel="noopener noreferrer" className="ml-2 text-indigo-600 hover:underline">
                    #{p.number} {p.title}
                  </a>
                ))}
              </li>
            ))}
          </ul>
        </div>
      )}
      {calendar && (
        <div>
          <div className="mb-1.5 font-medium text-slate-700">
            会议 · {calendar.count} 个 · {Math.round(calendar.total_minutes / 6) / 10} 小时
          </div>
          <ul className="space-y-1">
            {calendar.events.map((e) => (
              <li key={`${e.start}-${e.title}`} className="flex gap-3">
                <span className="w-24 font-mono text-xs text-slate-500">
                  {e.start}–{e.end}
                </span>
                <span>{e.title}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {!git && !calendar && Object.keys(notes).length === 0 && <p className="text-slate-400">没有连接 git 或日历。可在设置页连接。</p>}
      {Object.entries(notes).map(([k, v]) => (
        <div key={k} className="flex gap-2 text-xs text-amber-700">
          <Badge tone="amber">{k}</Badge>
          {v}
        </div>
      ))}
    </div>
  );
}
