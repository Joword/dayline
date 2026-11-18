// Server-side access to the Dayline API (BFF). The browser never sees the API token or
// the API address: route handlers and server components call the backend from here.

import { cookies } from "next/headers";
import { cache } from "react";
import { redirect } from "next/navigation";

import type { Me } from "./types";

export const TOKEN_COOKIE = "dl_token";
export const DEV_USER_COOKIE = "dl_user";

export function apiBase(): string {
  return (process.env.DAYLINE_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
}

export function timeZone(): string {
  return process.env.DAYLINE_TIMEZONE ?? "Asia/Shanghai";
}

/** Credentials for the backend, from the session cookies. */
export async function authHeaders(): Promise<Record<string, string>> {
  const jar = await cookies();
  const token = jar.get(TOKEN_COOKIE)?.value;
  if (token) return { authorization: `Bearer ${token}` };
  const devUser = jar.get(DEV_USER_COOKIE)?.value;
  // Only accepted by a backend in dev mode without a service token.
  if (devUser) return { "x-dayline-user": devUser };
  return {};
}

export async function backend<T>(path: string, init: RequestInit = {}): Promise<{ status: number; data: T | null }> {
  const res = await fetch(`${apiBase()}${path}`, {
    ...init,
    headers: { ...(await authHeaders()), ...(init.headers as Record<string, string> | undefined) },
    cache: "no-store",
  });
  const text = await res.text();
  let data: T | null = null;
  try {
    data = text ? (JSON.parse(text) as T) : null;
  } catch {
    data = null;
  }
  return { status: res.status, data };
}

/** The signed-in user, or a redirect to /login. Cached per request (layout + page share it). */
export const requireMe = cache(async function requireMe(): Promise<Me> {
  const jar = await cookies();
  if (!jar.get(TOKEN_COOKIE) && !jar.get(DEV_USER_COOKIE)) redirect("/login");
  const r = await backend<Me>("/me");
  if (r.status === 401 || r.status === 400 || r.status === 404 || !r.data) redirect("/login");
  return r.data;
});
