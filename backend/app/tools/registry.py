"""ToolRegistry: every tool the model can call, whatever its source.

Local tools are ``@tool``-decorated async functions in ``app/tools/local``. A
``ToolSource`` (member MCP servers, M7) lists its tools and the registry treats
them like local ones; sources are re-read periodically, since tools are enabled
and disabled while the process runs.

Per run the registry hands the model only the tools on the skill's whitelist that
the user may use (``for_run``), and ``execute`` re-checks that list at call time,
so a model that invents a tool name gets an error result, not an execution.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol

import jsonschema

from app.core.untrusted import wrap_untrusted
from app.llm.types import ToolCall, ToolResult, ToolSpec

log = logging.getLogger("dayline.tools")

MAX_RESULT_CHARS = 20_000


@dataclass
class ToolContext:
    """What a tool may know about the run. Identity comes from here, never from model input."""

    user_id: str
    lang: str = "zh"
    date: date | None = None
    relay: Any = None  # app.relay.service.Relay when storage is connected
    viewer: Any = None  # app.relay.policy.Viewer of the requesting user
    extras: dict[str, Any] = field(default_factory=dict)


ToolHandler = Callable[..., Awaitable[Any]]


@dataclass
class ToolOutput:
    """A handler result that can say it is an error (MCP ``isError``)."""

    text: str
    is_error: bool = False


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    source: Literal["local", "mcp"] = "local"
    timeout_s: float = 20.0
    permission: str | None = None  # a skill whitelisting this tool must list this permission
    # member tools: the scopes of their Relay grant, which the skill must also hold
    scopes: tuple[str, ...] = ()
    # False: the tool must not see manager data, so skills that read team data cannot use it
    manager_data: bool = True
    package: str | None = None
    # why the user may not use the tool (visibility, owner left, ...), or None
    available_to: Callable[[str], Awaitable[str | None]] | None = None

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, input_schema=self.input_schema)

    def required_permissions(self) -> list[str]:
        return sorted({*([self.permission] if self.permission else []), *self.scopes})


class ToolSource(Protocol):
    """A provider of tools other than in-process functions (MCP servers, M7)."""

    name: str

    async def list_tools(self) -> list[Tool]: ...

    def due(self) -> bool: ...


def reads_team_data(permissions: list[str]) -> bool:
    return any(p.startswith("read:team_") for p in permissions)


_LOCAL_TOOLS: dict[str, Tool] = {}


def tool(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    timeout_s: float = 20.0,
    permission: str | None = None,
):
    """Register an async function ``fn(ctx: ToolContext, **input)`` as a local tool."""

    def deco(fn: ToolHandler) -> ToolHandler:
        if name in _LOCAL_TOOLS:
            raise ValueError(f"duplicate local tool: {name}")
        jsonschema.Draft202012Validator.check_schema(input_schema)
        _LOCAL_TOOLS[name] = Tool(name, description, input_schema, fn, "local", timeout_s, permission)
        return fn

    return deco


def _import_local_tools() -> None:
    import app.tools.local as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"{pkg.__name__}.{mod.name}")


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        self._sources: dict[str, ToolSource] = {}
        self._from_source: dict[str, set[str]] = {}
        for t in tools or []:
            self.register(t)

    @classmethod
    def with_local_tools(cls) -> ToolRegistry:
        _import_local_tools()
        return cls(list(_LOCAL_TOOLS.values()))

    def register(self, t: Tool) -> None:
        if t.name in self._tools:
            raise ValueError(f"tool already registered: {t.name}")
        self._tools[t.name] = t

    async def add_source(self, source: ToolSource) -> None:
        self._sources[source.name] = source
        await self.sync(source.name)

    async def sync(self, source_name: str) -> None:
        """Replace a source's tools with what it lists now."""
        source = self._sources[source_name]
        try:
            fresh = await source.list_tools()
        except Exception:  # keep the previous set; a broken source must not break runs
            log.exception("tool source sync failed", extra={"source": source_name})
            return
        for name in self._from_source.pop(source_name, set()):
            self._tools.pop(name, None)
        names = set()
        for t in fresh:
            if t.name in self._tools:
                log.warning("tool name clash; skipped", extra={"tool": t.name, "source": source_name})
                continue
            self._tools[t.name] = t
            names.add(t.name)
        self._from_source[source_name] = names

    async def maybe_refresh(self) -> None:
        for name, source in self._sources.items():
            if source.due():
                await self.sync(name)

    async def for_run(
        self, whitelist: list[str], *, user_id: str, permissions: list[str]
    ) -> tuple[list[str], list[str]]:
        """(tools this run may offer, warnings about whitelisted tools left out)."""
        await self.maybe_refresh()
        allowed, warnings = [], []
        held = set(permissions)
        for name in whitelist:
            t = self._tools.get(name)
            if t is None:
                warnings.append(f"tool '{name}' is not available (not enabled)")
                continue
            if missing := sorted(set(t.required_permissions()) - held):
                warnings.append(f"tool '{name}' needs permissions {missing} that the skill does not declare")
                continue
            if not t.manager_data and reads_team_data(permissions):
                warnings.append(f"tool '{name}' may not receive manager data; this skill reads team data")
                continue
            if t.available_to is not None and (why := await t.available_to(user_id)):
                warnings.append(f"tool '{name}' is not available to you: {why}")
                continue
            allowed.append(name)
        return allowed, warnings

    def permissions(self) -> dict[str, str | None]:
        return {n: t.permission for n, t in self._tools.items()}

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def for_skill(self, whitelist: list[str]) -> list[Tool]:
        # whitelist order, so the tool list (and the prompt-cache prefix) is deterministic
        return [self._tools[n] for n in whitelist if n in self._tools]

    async def execute(self, call: ToolCall, ctx: ToolContext, allowed: set[str]) -> ToolResult:
        def err(msg: str) -> ToolResult:
            return ToolResult(tool_call_id=call.id, content=f"Error: {msg}", is_error=True)

        t = self._tools.get(call.name)
        if t is None or call.name not in allowed:
            log.warning("tool call rejected", extra={"tool": call.name, "reason": "not allowed"})
            return err(f"tool '{call.name}' is not available")
        try:
            jsonschema.validate(call.input, t.input_schema)
        except jsonschema.ValidationError as e:
            return err(f"invalid input: {e.message}")
        try:
            result = await asyncio.wait_for(t.handler(ctx, **call.input), timeout=t.timeout_s)
        except TimeoutError:
            return err(f"tool '{call.name}' timed out")
        except Exception as e:  # tool failures are reported to the model, not raised
            log.exception("tool failed", extra={"tool": call.name})
            return err(f"tool '{call.name}' failed: {type(e).__name__}")
        is_error = False
        if isinstance(result, ToolOutput):
            text, is_error = result.text, result.is_error
        elif isinstance(result, str):
            text = result
        else:
            text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n...[truncated]"
        return ToolResult(
            tool_call_id=call.id,
            content=wrap_untrusted(text, kind="tool_result", name=call.name),
            is_error=is_error,
        )
