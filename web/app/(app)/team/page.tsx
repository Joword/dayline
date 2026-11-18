import { redirect } from "next/navigation";

import { TeamBoard } from "@/components/TeamBoard";
import { PageTitle } from "@/components/ui";
import { todayIn } from "@/lib/form";
import { requireMe, timeZone } from "@/lib/server";

export default async function TeamPage() {
  const me = await requireMe();
  if (me.direct_reports.length === 0) redirect("/");
  return (
    <>
      <PageTitle title="团队" subtitle="下属的提交情况、逐个审阅、生成团队汇总并向上汇报。" />
      <TeamBoard today={todayIn(timeZone())} />
    </>
  );
}
