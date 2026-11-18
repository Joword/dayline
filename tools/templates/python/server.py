"""Dayline member tool - Python template.

An MCP server over streamable HTTP. Dayline calls it on the internal `tools` network with
``Authorization: Bearer $DAYLINE_TOOL_TOKEN``; anything else gets 401.

Each tools/call from Dayline carries in ``_meta``:
  dayline/grant    short-lived token for the Dayline API (the user + this tool's permissions)
  dayline/api_url  where to send it, e.g. http://api:8000
  dayline/lang     zh | en
  dayline/date     the report date (YYYY-MM-DD)

Local debugging without Dayline:
  DAYLINE_TOOL_DEV=1 python server.py        # no auth; then MCP Inspector or `dl tools check`
"""

from __future__ import annotations

import hmac
import os
from typing import Annotated, Any

import httpx
import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

NAME = os.environ.get("DAYLINE_TOOL_NAME", "example-tool")

mcp = MCPServer(NAME, version="0.1.0")


def _dayline(ctx: Context) -> tuple[str | None, str | None]:
    meta = ctx.request_context.meta or {}
    return meta.get("dayline/grant"), meta.get("dayline/api_url")


@mcp.tool(description="Count the characters, words and lines of a piece of text.")
def count_words(text: Annotated[str, Field(max_length=20000, description="The text to count")]) -> str:
    return f"characters: {len(text)}, words: {len(text.split())}, lines: {len(text.splitlines()) or 1}"


@mcp.tool(
    description=(
        "List the plan items ('plan' kind) from the current user's most recent submitted daily reports, "
        "newest first."
    )
)
async def my_recent_plans(
    ctx: Context,
    days: Annotated[int, Field(ge=1, le=7, description="How many recent reports to read")] = 3,
) -> str:
    grant, api = _dayline(ctx)
    if not grant or not api:
        return "not available: this call did not come from Dayline"
    async with httpx.AsyncClient(
        base_url=api, headers={"Authorization": f"Bearer {grant}"}, timeout=10, trust_env=False
    ) as c:
        r = await c.get("/internal/tool/reports", params={"limit": days})
    if r.status_code != 200:
        return f"not available: Dayline answered {r.status_code}"
    lines = []
    for report in r.json():
        plans = [i["text"] for i in report.get("own_items", []) if i.get("kind") == "plan"]
        lines.append(f"{report['date']}: " + ("; ".join(plans) if plans else "(no plan items)"))
    return "\n".join(lines) or "no submitted reports yet"


class BearerAuth:
    """Only Dayline (which holds this tool's token) may call the server."""

    def __init__(self, app: Any, token: str | None):
        self.app = app
        self.expected = f"Bearer {token}".encode() if token else None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.expected is not None:
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def build_app() -> Any:
    token = os.environ.get("DAYLINE_TOOL_TOKEN")
    if not token and os.environ.get("DAYLINE_TOOL_DEV") != "1":
        raise SystemExit(
            "DAYLINE_TOOL_TOKEN is not set (DAYLINE_TOOL_DEV=1 runs without auth for local debugging)"
        )
    hosts = [f"tool-{NAME}:*", "localhost:*", "127.0.0.1:*"]
    hosts += [h for h in os.environ.get("DAYLINE_TOOL_HOSTS", "").split(",") if h]
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts
        ),
    )
    return BearerAuth(app, token)


if __name__ == "__main__":
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")
