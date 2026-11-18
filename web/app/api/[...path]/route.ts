// BFF proxy: /api/<path> -> Dayline API, with the session's credentials added server-side.

import { NextRequest, NextResponse } from "next/server";

import { backendPath, sameOrigin } from "@/lib/proxy";
import { apiBase, authHeaders } from "@/lib/server";

async function proxy(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const target = backendPath(path);
  if (!target) return NextResponse.json({ detail: "not found" }, { status: 404 });
  if (req.method !== "GET" && !sameOrigin(req.headers.get("origin"), req.headers.get("host"))) {
    return NextResponse.json({ detail: "cross-origin request refused" }, { status: 403 });
  }
  const headers: Record<string, string> = { ...(await authHeaders()) };
  const rid = req.headers.get("x-request-id");
  if (rid) headers["x-request-id"] = rid;
  let body: string | undefined;
  if (req.method !== "GET") {
    body = await req.text();
    headers["content-type"] = "application/json";
  }
  let res: Response;
  try {
    res = await fetch(`${apiBase()}${target}${req.nextUrl.search}`, { method: req.method, headers, body, cache: "no-store" });
  } catch {
    return NextResponse.json({ detail: "Dayline API is not reachable" }, { status: 502 });
  }
  return new NextResponse(await res.text(), {
    status: res.status,
    headers: { "content-type": res.headers.get("content-type") ?? "application/json" },
  });
}

export { proxy as DELETE, proxy as GET, proxy as POST };
