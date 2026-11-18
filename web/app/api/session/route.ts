// Sign in / out. The token is verified against the API, then kept in an httpOnly cookie.

import { NextRequest, NextResponse } from "next/server";

import { sameOrigin } from "@/lib/proxy";
import { apiBase, DEV_USER_COOKIE, TOKEN_COOKIE } from "@/lib/server";

const MAX_AGE = 30 * 24 * 3600;

function cookieOpts() {
  return {
    httpOnly: true,
    sameSite: "lax" as const,
    secure: process.env.NODE_ENV === "production" && process.env.DAYLINE_INSECURE_COOKIES !== "1",
    path: "/",
    maxAge: MAX_AGE,
  };
}

export async function POST(req: NextRequest) {
  if (!sameOrigin(req.headers.get("origin"), req.headers.get("host"))) {
    return NextResponse.json({ detail: "cross-origin request refused" }, { status: 403 });
  }
  const { token, user } = (await req.json().catch(() => ({}))) as { token?: string; user?: string };
  const headers: Record<string, string> = token
    ? { authorization: `Bearer ${token.trim()}` }
    : user
      ? { "x-dayline-user": user.trim() } // dev only: accepted when the API runs without auth
      : {};
  if (!token && !user) return NextResponse.json({ detail: "token required" }, { status: 400 });
  let me: Response;
  try {
    me = await fetch(`${apiBase()}/me`, { headers, cache: "no-store" });
  } catch {
    return NextResponse.json({ detail: "Dayline API is not reachable" }, { status: 502 });
  }
  if (!me.ok) {
    const detail = me.status === 404 ? "该用户不在组织中" : token ? "token 无效或已吊销" : "开发登录不可用(API 需要 token)";
    return NextResponse.json({ detail }, { status: 401 });
  }
  const res = NextResponse.json(await me.json());
  res.cookies.delete(token ? DEV_USER_COOKIE : TOKEN_COOKIE);
  res.cookies.set(token ? TOKEN_COOKIE : DEV_USER_COOKIE, (token ?? user ?? "").trim(), cookieOpts());
  return res;
}

export async function DELETE(req: NextRequest) {
  if (!sameOrigin(req.headers.get("origin"), req.headers.get("host"))) {
    return NextResponse.json({ detail: "cross-origin request refused" }, { status: 403 });
  }
  const res = NextResponse.json({ ok: true });
  res.cookies.delete(TOKEN_COOKIE);
  res.cookies.delete(DEV_USER_COOKIE);
  return res;
}
