import Link from "next/link";

import { Badge, Card, PageTitle } from "@/components/ui";
import { backend, requireMe } from "@/lib/server";
import type { SkillInfo } from "@/lib/types";

const LAYER: Record<string, "indigo" | "green" | "slate"> = { builtin: "indigo", team: "green", user: "slate" };

export default async function SkillsPage() {
  const me = await requireMe();
  const isManager = me.direct_reports.length > 0;
  const r = await backend<SkillInfo[]>("/skills");
  const skills = (r.data ?? []).filter((s) => isManager || s.roles.includes("member"));
  return (
    <>
      <PageTitle title="Skills" subtitle="所有可用的 skill。表单按 skill.yaml 自动生成;在 skills/team 下新建目录即可增加。" />
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {skills.map((s) => (
          <Link key={s.name} href={`/skills/${s.name}`} className="group">
            <Card className="h-full transition group-hover:border-indigo-300 group-hover:shadow">
              <div className="mb-2 flex items-center justify-between">
                <span className="font-mono text-sm font-semibold text-slate-800">{s.name}</span>
                <Badge tone={LAYER[s.layer] ?? "slate"}>{s.layer}</Badge>
              </div>
              <p className="text-sm text-slate-600">{s.description}</p>
              <p className="mt-3 text-xs text-slate-400">
                v{s.version} · {s.langs.join(" / ")}
                {s.commands.length > 0 && ` · ${s.commands[0]}`}
              </p>
            </Card>
          </Link>
        ))}
      </div>
    </>
  );
}
