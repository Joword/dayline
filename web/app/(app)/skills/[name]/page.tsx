import Link from "next/link";
import { notFound } from "next/navigation";

import { SkillForm } from "@/components/SkillForm";
import { Alert, PageTitle } from "@/components/ui";
import { todayIn } from "@/lib/form";
import { backend, requireMe, timeZone } from "@/lib/server";
import type { SkillInfo } from "@/lib/types";

export default async function SkillPage({ params }: { params: Promise<{ name: string }> }) {
  const { name } = await params;
  const me = await requireMe();
  const r = await backend<SkillInfo>(`/skills/${encodeURIComponent(name)}`);
  if (r.status === 404 || !r.data) notFound();
  const skill = r.data;
  const allowed = skill.roles.includes("member") || me.direct_reports.length > 0;
  return (
    <>
      <PageTitle title={skill.name} subtitle={skill.description} />
      <Link href="/skills" className="mb-4 inline-block text-sm text-indigo-600 hover:underline">
        ← 全部 skills
      </Link>
      {allowed ? <SkillForm skill={skill} today={todayIn(timeZone())} /> : <Alert tone="warn">这个 skill 只对有直属下属的管理者开放。</Alert>}
    </>
  );
}
