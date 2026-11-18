import { ActivityPanel } from "@/components/ActivityPanel";
import { GenerateFlow } from "@/components/GenerateFlow";
import { Card, PageTitle } from "@/components/ui";
import { todayIn } from "@/lib/form";
import { backend, timeZone } from "@/lib/server";
import type { Activity } from "@/lib/types";

export default async function GeneratePage() {
  const today = todayIn(timeZone());
  const activity = await backend<Activity>(`/me/activity?date=${today}`);
  return (
    <>
      <PageTitle title="写日报" subtitle="把自述整理成结构化日报,并对照上次的计划;确认后才提交给上级。" />
      <GenerateFlow today={today} />
      <div className="mt-6">
        <Card title="今天的 git / 日历(会自动用于生成)">
          <ActivityPanel activity={activity.status === 200 ? activity.data : null} />
        </Card>
      </div>
    </>
  );
}
