"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { api } from "@/lib/client";
import type { Escalation } from "@/lib/types";

import { Alert, Button, inputCls, Label } from "./ui";

/** Actions on one escalation: only the current holder may accept / forward / resolve. */
export function EscalationActions({ esc, me, hasManager }: { esc: Escalation; me: string; hasManager: boolean }) {
  const router = useRouter();
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const holder = esc.current_holder_id === me && esc.status !== "resolved";

  async function act(action: "accept" | "escalate" | "resolve" | "comment") {
    setBusy(action);
    setError(null);
    try {
      await api(`/escalations/${esc.id}/${action}`, { body: { note: note || null } });
      setNote("");
      router.refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-3">
      <label className="block">
        <Label hint={holder ? "解决时必填,其他操作可选" : undefined}>备注</Label>
        <textarea className={`${inputCls} min-h-20`} value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      {error && <Alert tone="error">{error}</Alert>}
      <div className="flex flex-wrap gap-2">
        {holder && esc.status !== "in_progress" && (
          <Button onClick={() => act("accept")} loading={busy === "accept"}>
            接手
          </Button>
        )}
        {holder && (
          <Button variant="secondary" onClick={() => act("resolve")} loading={busy === "resolve"} disabled={!note.trim()}>
            就地解决
          </Button>
        )}
        {holder && hasManager && (
          <Button variant="secondary" onClick={() => act("escalate")} loading={busy === "escalate"}>
            继续上报
          </Button>
        )}
        <Button variant="ghost" onClick={() => act("comment")} loading={busy === "comment"} disabled={!note.trim()}>
          补充信息
        </Button>
      </div>
    </div>
  );
}
