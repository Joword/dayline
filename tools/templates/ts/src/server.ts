/**
 * Dayline member tool - TypeScript template.
 *
 * An MCP server over streamable HTTP. Dayline calls it on the internal `tools` network with
 * `Authorization: Bearer $DAYLINE_TOOL_TOKEN`; anything else gets 401.
 *
 * Each tools/call from Dayline carries in `_meta`:
 *   dayline/grant    short-lived token for the Dayline API (the user + this tool's permissions)
 *   dayline/api_url  where to send it, e.g. http://api:8000
 *   dayline/lang     zh | en
 *   dayline/date     the report date (YYYY-MM-DD)
 *
 * Local debugging without Dayline:
 *   DAYLINE_TOOL_DEV=1 npm start        # no auth; then MCP Inspector or `dl tools check`
 */
import { timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";

import { hostHeaderValidation, toNodeHandler } from "@modelcontextprotocol/node";
import { createMcpHandler, McpServer } from "@modelcontextprotocol/server";
import * as z from "zod/v4";

const NAME = process.env.DAYLINE_TOOL_NAME ?? "example-tool-ts";

function dayline(meta: Record<string, unknown> | undefined): { grant?: string; api?: string } {
  return { grant: meta?.["dayline/grant"] as string | undefined, api: meta?.["dayline/api_url"] as string | undefined };
}

function buildServer(): McpServer {
  const server = new McpServer({ name: NAME, version: "0.1.0" });

  server.registerTool(
    "count_words",
    {
      description: "Count the characters, words and lines of a piece of text.",
      inputSchema: z.object({ text: z.string().max(20000).describe("The text to count") }),
    },
    async ({ text }) => {
      const words = text.split(/\s+/).filter(Boolean).length;
      const lines = text.split(/\r?\n/).length;
      return { content: [{ type: "text", text: `characters: ${[...text].length}, words: ${words}, lines: ${lines}` }] };
    },
  );

  server.registerTool(
    "my_recent_plans",
    {
      description:
        "List the plan items ('plan' kind) from the current user's most recent submitted daily reports, newest first.",
      inputSchema: z.object({ days: z.number().int().min(1).max(7).default(3).describe("How many recent reports to read") }),
    },
    async ({ days }, ctx) => {
      const { grant, api } = dayline(ctx.mcpReq._meta as Record<string, unknown> | undefined);
      if (!grant || !api) return { content: [{ type: "text", text: "not available: this call did not come from Dayline" }] };
      const r = await fetch(`${api}/internal/tool/reports?limit=${days}`, {
        headers: { Authorization: `Bearer ${grant}` },
        signal: AbortSignal.timeout(10_000),
      });
      if (!r.ok) return { content: [{ type: "text", text: `not available: Dayline answered ${r.status}` }] };
      const reports = (await r.json()) as { date: string; own_items: { kind: string; text: string }[] }[];
      const lines = reports.map((rep) => {
        const plans = rep.own_items.filter((i) => i.kind === "plan").map((i) => i.text);
        return `${rep.date}: ${plans.length ? plans.join("; ") : "(no plan items)"}`;
      });
      return { content: [{ type: "text", text: lines.join("\n") || "no submitted reports yet" }] };
    },
  );

  return server;
}

const token = process.env.DAYLINE_TOOL_TOKEN;
if (!token && process.env.DAYLINE_TOOL_DEV !== "1") {
  console.error("DAYLINE_TOOL_TOKEN is not set (DAYLINE_TOOL_DEV=1 runs without auth for local debugging)");
  process.exit(1);
}
const expected = token ? Buffer.from(`Bearer ${token}`) : null;
const authorized = (header: string | undefined): boolean => {
  if (!expected) return true;
  const got = Buffer.from(header ?? "");
  return got.length === expected.length && timingSafeEqual(got, expected);
};

const hosts = [`tool-${NAME}`, "localhost", "127.0.0.1", ...(process.env.DAYLINE_TOOL_HOSTS ?? "").split(",").filter(Boolean)];
const validateHost = hostHeaderValidation(hosts);
const handle = toNodeHandler(createMcpHandler(() => buildServer()));
const port = Number(process.env.PORT ?? 8080);

createServer((req, res) => {
  if (!authorized(req.headers.authorization)) {
    res.writeHead(401, { "content-type": "text/plain" }).end("unauthorized");
    return;
  }
  if (!validateHost(req, res)) return;
  if (!req.url?.startsWith("/mcp")) {
    res.writeHead(404).end();
    return;
  }
  void handle(req, res);
}).listen(port, "0.0.0.0", () => console.log(`${NAME} listening on :${port}/mcp`));
