import { SettingsPanel } from "@/components/SettingsPanel";
import { PageTitle } from "@/components/ui";
import { backend, requireMe } from "@/lib/server";
import type { Integration } from "@/lib/types";

export default async function SettingsPage() {
  const me = await requireMe();
  const integrations = await backend<Integration[]>("/me/integrations");
  return (
    <>
      <PageTitle title="设置" subtitle="数据源、授权和 API token" />
      <SettingsPanel me={me} integrations={integrations.data ?? []} />
    </>
  );
}
