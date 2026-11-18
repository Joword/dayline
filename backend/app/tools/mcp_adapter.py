"""MCP adapter: enabled member tools as a ``ToolSource`` of the ``ToolRegistry`` (plan §8).

* The model sees the definition approved at review time (``ToolDoc.definition``), never
  what the server reports now.
* Every call first lists the server's tools and compares the hash with the approved one;
  on a mismatch the call is refused and the tool is disabled (``on_definition_changed``).
* Each call carries a short-lived grant in ``_meta`` (user + the tool's approved scopes)
  that the tool can present to ``/internal/tool/*``; nothing else about the user is sent.
* The result goes back to the model wrapped as untrusted (``ToolRegistry.execute``).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import Implementation

from app import __version__
from app.db.models import ToolDoc
from app.db.repositories.tools import ToolsRepo
from app.db.repositories.users import UsersRepo
from app.tools.contract import definition_hash, full_name, normalize
from app.tools.registry import Tool, ToolContext, ToolOutput
from app.tools.tokens import mint_grant, tool_token

log = logging.getLogger("dayline.tools.mcp")

META_GRANT = "dayline/grant"
META_API = "dayline/api_url"
META_LANG = "dayline/lang"
META_DATE = "dayline/date"
GRANT_SLACK_S = 30
CONNECT_TIMEOUT_S = 5.0
MAX_LIST_PAGES = 20

OpenClient = Callable[[ToolDoc], Client]


@asynccontextmanager
async def open_url(url: str, *, token: str | None, timeout_s: float) -> AsyncIterator[Client]:
    """An MCP session over streamable HTTP, with ``token`` as the bearer."""
    http = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"} if token else {},
        timeout=httpx2.Timeout(timeout_s, connect=CONNECT_TIMEOUT_S),
        trust_env=False,  # the tools network is internal: never through HTTPS_PROXY
    )
    try:
        async with Client(
            streamable_http_client(url, http_client=http),
            cache=None,
            read_timeout_seconds=timeout_s,
            client_info=Implementation(name="dayline", version=__version__),
        ) as c:
            yield c
    finally:
        await http.aclose()


class McpConnector:
    """Opens MCP sessions to tool containers (streamable HTTP with the tool's own token)."""

    def __init__(self, secret: str, *, open_client: OpenClient | None = None):
        self.secret = secret
        self._open = open_client  # tests: in-process servers

    @asynccontextmanager
    async def session(self, doc: ToolDoc, timeout_s: float) -> AsyncIterator[Client]:
        if self._open is not None:
            async with self._open(doc) as c:
                yield c
            return
        async with open_url(doc.endpoint, token=tool_token(self.secret, doc.name), timeout_s=timeout_s) as c:
            yield c

    async def definition(self, doc: ToolDoc, timeout_s: float = 10.0) -> list[dict[str, Any]]:
        async with self.session(doc, timeout_s) as c:
            return await list_definition(c)

    async def call_with(
        self, doc: ToolDoc, tool: str, args: dict[str, Any], meta: dict[str, Any] | None
    ) -> tuple[str, bool, str]:
        """(text, is_error, live definition hash). The call is skipped when the hash differs."""
        async with self.session(doc, doc.timeout_s) as c:
            live = definition_hash(await list_definition(c))
            if live != doc.definition_hash:
                return "", True, live
            r = await c.call_tool(tool, args, read_timeout_seconds=doc.timeout_s, meta=meta)
            return result_text(r), bool(r.is_error), live


async def list_definition(c: Client) -> list[dict[str, Any]]:
    tools: list[Any] = []
    cursor = None
    for _ in range(MAX_LIST_PAGES):
        page = await c.list_tools(cursor=cursor)
        tools += page.tools
        cursor = page.next_cursor
        if not cursor:
            break
    return normalize(tools)


def result_text(r: Any) -> str:
    parts = []
    for block in r.content or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        else:
            parts.append(f"[{getattr(block, 'type', 'unknown')} content omitted]")
    if not parts and r.structured_content is not None:
        parts.append(json.dumps(r.structured_content, ensure_ascii=False, default=str))
    return "\n".join(parts)


Availability = Callable[[ToolDoc, str], Awaitable[str | None]]
ChangedHook = Callable[[ToolDoc, str], Awaitable[None]]


class McpToolSource:
    name = "mcp"

    def __init__(
        self,
        *,
        repo: ToolsRepo,
        users: UsersRepo,
        connector: McpConnector,
        api_url: str,
        availability: Availability,
        sync_interval_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.repo = repo
        self.users = users
        self.connector = connector
        self.api_url = api_url.rstrip("/")
        self.availability = availability
        self.on_definition_changed: ChangedHook | None = None
        self.sync_interval_s = sync_interval_s
        self.clock = clock
        self._last_sync: float | None = None
        self._blocked: set[str] = set()  # disabled since the last sync

    def invalidate(self) -> None:
        self._last_sync = None

    def due(self) -> bool:
        return self._last_sync is None or self.clock() - self._last_sync >= self.sync_interval_s

    async def list_tools(self) -> list[Tool]:
        self._last_sync = self.clock()
        self._blocked.clear()
        out = []
        for doc in await self.repo.list(["enabled"]):
            for item in doc.definition:
                out.append(
                    Tool(
                        name=full_name(doc.name, item["name"]),
                        description=item.get("description", ""),
                        input_schema=item.get("inputSchema") or {"type": "object"},
                        handler=partial(self._call, doc, item["name"]),
                        source="mcp",
                        timeout_s=doc.timeout_s + 2 * CONNECT_TIMEOUT_S,
                        scopes=tuple(doc.permissions),
                        manager_data=doc.manager_data,
                        package=doc.name,
                        available_to=partial(self._available, doc),
                    )
                )
        return out

    async def _available(self, doc: ToolDoc, user_id: str) -> str | None:
        if doc.name in self._blocked:
            return "the tool was disabled"
        return await self.availability(doc, user_id)

    async def _call(self, doc: ToolDoc, tool: str, ctx: ToolContext, /, **args: Any) -> ToolOutput:
        # positional-only: member tools may name their own arguments `tool`, `doc` or `ctx`
        if doc.name in self._blocked:
            return ToolOutput("tool unavailable: it was disabled", is_error=True)
        grant = mint_grant(
            self.connector.secret,
            tool=doc.name,
            user_id=ctx.user_id,
            scopes=doc.permissions,
            ttl_s=doc.timeout_s + GRANT_SLACK_S,
            run_id=ctx.extras.get("run_id"),
        )
        meta = {
            META_GRANT: grant,
            META_API: self.api_url,
            META_LANG: ctx.lang,
            META_DATE: str(ctx.date) if ctx.date else None,
        }
        text, is_error, live = await self.connector.call_with(doc, tool, args, meta)
        if live != doc.definition_hash:
            self._blocked.add(doc.name)
            log.warning(
                "tool definition changed; call refused",
                extra={"tool": doc.name, "approved": doc.definition_hash, "live": live},
            )
            if self.on_definition_changed is not None:
                await self.on_definition_changed(doc, live)
            return ToolOutput("tool unavailable: its definition changed since it was approved", is_error=True)
        return ToolOutput(text, is_error=is_error)
