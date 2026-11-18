"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { Alert, Button, Card, inputCls, Label } from "@/components/ui";

export default function LoginPage() {
  const router = useRouter();
  const [token, setToken] = useState("");
  const [user, setUser] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function signIn(body: { token?: string; user?: string }) {
    setBusy(true);
    setError(null);
    const res = await fetch("/api/session", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    setBusy(false);
    if (res.ok) {
      router.replace("/");
      router.refresh();
    } else {
      setError(((await res.json().catch(() => ({}))) as { detail?: string }).detail ?? "登录失败");
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (token.trim()) signIn({ token });
    else if (user.trim()) signIn({ user });
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-md">
        <div className="mb-6 text-center">
          <div className="text-2xl font-semibold text-slate-900">Dayline</div>
          <p className="mt-1 text-sm text-slate-500">日报助手</p>
        </div>
        <Card>
          <form onSubmit={submit} className="space-y-4">
            <label className="block">
              <Label hint="dl token create <user> 或设置页生成">个人 token</Label>
              <input className={inputCls} type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} placeholder="dl_..." />
            </label>
            <details className="text-sm text-slate-500">
              <summary className="cursor-pointer select-none">开发环境:按用户 id 登录</summary>
              <p className="mt-2 text-xs">只在 API 以开发模式运行且未配置 token 时可用。</p>
              <input className={`${inputCls} mt-2`} value={user} onChange={(e) => setUser(e.target.value)} placeholder="zhangsan" />
            </details>
            {error && <Alert tone="error">{error}</Alert>}
            <Button type="submit" loading={busy} className="w-full justify-center">
              登录
            </Button>
          </form>
        </Card>
      </div>
    </main>
  );
}
