import Link from "next/link";
import { redirect } from "next/navigation";

import { MemberReview } from "@/components/MemberReview";
import { Alert, PageTitle } from "@/components/ui";
import { todayIn } from "@/lib/form";
import { requireMe, timeZone } from "@/lib/server";

export default async function TeamReviewPage({ searchParams }: { searchParams: Promise<{ member?: string; date?: string }> }) {
  const me = await requireMe();
  if (me.direct_reports.length === 0) redirect("/");
  const { member, date } = await searchParams;
  const target = me.direct_reports.find((u) => u.id === member);
  return (
    <>
      <PageTitle title="审阅下属日报" />
      <Link href="/team" className="mb-4 inline-block text-sm text-indigo-600 hover:underline">
        ← 返回团队
      </Link>
      {target ? (
        <MemberReview member={target.id} name={target.name} date={date || todayIn(timeZone())} />
      ) : (
        <Alert tone="warn">只能审阅你的直属下属。</Alert>
      )}
    </>
  );
}
