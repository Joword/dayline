"use client";

import { useRouter } from "next/navigation";
import { type ChangeEvent, useState } from "react";

import { api } from "@/lib/client";
import type { Integration, Me } from "@/lib/types";

import { Alert, Badge, Button, Card, inputCls, Label } from "./ui";

type Kind = "github" | "gitlab" | "outlook-ics" | "coremail-caldav" | "coremail-imap";

function IntegrationForm({ onAdded }: { onAdded: () => void }) {
  const [kind, setKind] = useState<Kind>("gitlab");
  const [v, setV] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (k: string) => (e: ChangeEvent<HTMLInputElement>) => setV((s) => ({ ...s, [k]: e.target.value }));

  function config(): Record<string, unknown> {
    const common = v.ca_bundle ? { ca_bundle: v.ca_bundle } : {};
    switch (kind) {
      case "github":
      case "gitlab":
        return {
          type: "git",
          provider: kind,
          base_url: v.base_url || null,
          username: v.username || null,
          emails: (v.emails ?? "").split(/[,\s]+/).filter(Boolean),
          token: v.token,
          ...common,
        };
      case "outlook-ics":
        return { type: "calendar", provider: "outlook", mode: "ics", ics_url: v.ics_url, email: v.email || null, ...common };
      case "coremail-caldav":
        return { type: "calendar", provider: "coremail", mode: "caldav", caldav_url: v.caldav_url, username: v.username, password: v.password, email: v.email || null, ...common };
      case "coremail-imap":
        return { type: "calendar", provider: "coremail", mode: "imap", imap_host: v.imap_host, username: v.username, password: v.password, email: v.email || null, ...common };
    }
  }

  async function add() {
    setBusy(true);
    setError(null);
    try {
      await api("/me/integrations", { body: { config: config() } });
      setV({});
      onAdded();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const input = (k: string, label: string, opts: { type?: string; hint?: string; required?: boolean } = {}) => (
    <label key={k} className="block">
      <Label hint={opts.hint} required={opts.required}>
        {label}
      </Label>
      <input className={inputCls} type={opts.type ?? "text"} autoComplete="off" value={v[k] ?? ""} onChange={set(k)} />
    </label>
  );

  return (
    <div className="space-y-3 rounded-lg border border-slate-200 p-4">
      <label className="block">
        <Label>类型</Label>
        <select className={inputCls} value={kind} onChange={(e) => { setKind(e.target.value as Kind); setV({}); }}>
          <option value="gitlab">GitLab(自建或 gitlab.com)</option>
          <option value="github">GitHub</option>
          <option value="outlook-ics">Outlook 日历(发布的 ICS 链接)</option>
          <option value="coremail-caldav">Coremail 日历(CalDAV)</option>
          <option value="coremail-imap">Coremail 会议邀请(IMAP)</option>
        </select>
      </label>
      {(kind === "github" || kind === "gitlab") && (
        <>
          {input("base_url", "地址", { hint: kind === "gitlab" ? "如 https://gitlab.corp" : "GitHub Enterprise 填 API 地址,否则留空" })}
          {input("username", "用户名", { required: kind === "github" })}
          {input("emails", "提交邮箱", { hint: "多个用逗号分隔" })}
          {input("token", "只读 token", { type: "password", required: true, hint: kind === "gitlab" ? "read_api 权限" : "fine-grained,只读" })}
        </>
      )}
      {kind === "outlook-ics" && input("ics_url", "ICS 链接", { type: "password", required: true, hint: "Outlook 设置 → 共享日历 → 发布" })}
      {(kind === "coremail-caldav" || kind === "coremail-imap") && (
        <>
          {kind === "coremail-caldav" ? input("caldav_url", "CalDAV 地址", { required: true }) : input("imap_host", "IMAP 服务器", { required: true })}
          {input("username", "邮箱账号", { required: true })}
          {input("password", "客户端专用密码 / 授权码", { type: "password", required: true, hint: "不要填邮箱登录密码" })}
        </>
      )}
      {kind !== "github" && kind !== "gitlab" && input("email", "我的邮箱", { hint: "用于识别你已拒绝的会议" })}
      {input("ca_bundle", "公司 CA 证书路径", { hint: "可选,服务器上的路径" })}
      {error && <Alert tone="error">{error}</Alert>}
      <Button onClick={add} loading={busy}>
        添加
      </Button>
    </div>
  );
}

export function SettingsPanel({ me, integrations }: { me: Me; integrations: Integration[] }) {
  const router = useRouter();
  const [newToken, setNewToken] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const refresh = () => router.refresh();

  async function call(path: string, method: string, body?: unknown) {
    setError(null);
    try {
      const r = await api<Record<string, unknown>>(path, { method, body });
      refresh();
      return r;
    } catch (e) {
      setError((e as Error).message);
      return null;
    }
  }

  const consented = new Set(me.consents.map((c) => c.scope));

  return (
    <div className="space-y-6">
      {error && <Alert tone="error">{error}</Alert>}

      <Card title="数据源" actions={<Button variant="secondary" onClick={() => setAdding((a) => !a)}>{adding ? "收起" : "添加"}</Button>}>
        <p className="mb-3 text-xs text-slate-500">只使用只读凭证,加密保存,随时可删除。生成日报时会自动带上当天的提交和会议。</p>
        {adding && (
          <div className="mb-4">
            <IntegrationForm onAdded={() => { setAdding(false); refresh(); }} />
          </div>
        )}
        <ul className="divide-y divide-slate-100 text-sm">
          {integrations.length === 0 && <li className="py-2 text-slate-400">还没有连接。</li>}
          {integrations.map((d) => (
            <li key={d.id} className="flex items-center justify-between gap-3 py-2.5">
              <div>
                <span className="font-medium">{d.label}</span>{" "}
                <Badge tone={d.status === "active" ? "green" : "red"}>{d.status === "active" ? "正常" : "出错"}</Badge>
                {d.mode_used && <span className="ml-2 text-xs text-slate-400">via {d.mode_used}</span>}
                {d.last_error && <p className="mt-0.5 text-xs text-rose-600">{d.last_error}</p>}
              </div>
              <Button variant="ghost" onClick={() => call(`/me/integrations/${d.id}`, "DELETE")}>
                删除
              </Button>
            </li>
          ))}
        </ul>
      </Card>

      <Card title="授权直属上级">
        <p className="mb-3 text-xs text-slate-500">
          授权后,你的直属上级{me.manager ? `(${me.manager.name})` : ""}审阅你的日报时可以对照你的 git / 日历,标出不一致之处。授权只对当前直属上级有效,换上级后自动失效。
        </p>
        {!me.manager && <Alert tone="info">你没有直属上级。</Alert>}
        {me.manager && (
          <div className="flex flex-wrap gap-6 text-sm">
            {(["git", "calendar"] as const).map((scope) => (
              <label key={scope} className="flex items-center gap-2">
                <input
                  type="checkbox"
                  className="h-4 w-4"
                  checked={consented.has(scope)}
                  onChange={(e) => call(e.target.checked ? "/me/consents" : `/me/consents/${scope}`, e.target.checked ? "POST" : "DELETE", e.target.checked ? { scope } : undefined)}
                />
                {scope === "git" ? "git 提交和 PR" : "日历会议"}
              </label>
            ))}
          </div>
        )}
      </Card>

      <Card title="API token">
        <p className="mb-3 text-xs text-slate-500">用于 CLI、VSCode 等客户端。token 只显示一次。</p>
        {newToken && (
          <div className="mb-3">
            <Alert tone="ok">
              新 token(只显示这一次):<code className="ml-1 break-all font-mono">{newToken}</code>
            </Alert>
          </div>
        )}
        <div className="mb-3 flex gap-2">
          <input className={inputCls} placeholder="用途,如 laptop" value={label} onChange={(e) => setLabel(e.target.value)} />
          <Button
            variant="secondary"
            onClick={async () => {
              const r = await call("/me/tokens", "POST", { label });
              if (r) {
                setNewToken(String(r.token));
                setLabel("");
              }
            }}
          >
            生成
          </Button>
        </div>
        <ul className="divide-y divide-slate-100 text-sm">
          {me.tokens.map((t) => (
            <li key={t.id} className="flex items-center justify-between py-2">
              <span>
                <span className="font-mono text-xs">{t.id}</span> {t.label}
                <span className="ml-2 text-xs text-slate-400">{t.last_used_at ? `最近使用 ${t.last_used_at.slice(0, 10)}` : "未使用"}</span>
              </span>
              <Button variant="ghost" onClick={() => call(`/me/tokens/${t.id}`, "DELETE")}>
                吊销
              </Button>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
