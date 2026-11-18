import type { ReactNode } from "react";

import { Nav } from "@/components/Nav";
import { backend, requireMe } from "@/lib/server";
import type { Escalation } from "@/lib/types";

export default async function AppLayout({ children }: { children: ReactNode }) {
  const me = await requireMe();
  const [notes, held] = await Promise.all([
    backend<{ unread: number }>("/me/notifications?unread=true"),
    backend<Escalation[]>("/escalations?scope=held"),
  ]);
  return (
    <>
      <Nav name={me.user.name} isManager={me.direct_reports.length > 0} unread={notes.data?.unread ?? 0} held={held.data?.length ?? 0} />
      <main className="mx-auto max-w-6xl px-4 py-8">{children}</main>
    </>
  );
}
