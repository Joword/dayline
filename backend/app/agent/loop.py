"""Tool loop with a hard turn limit.

The loop is deliberately small: call the model, run the tools it asked for (all
results of one turn go back in one user message), repeat. When the tool budget
is used up the next call sends ``tool_choice: none`` so the model must answer.
The budget shrinks when a backup endpoint with its own ``max_tool_turns``
serves a turn.
"""

from __future__ import annotations

import asyncio
import time

from pydantic import BaseModel, Field

from app.llm.gateway import LLMGateway
from app.llm.types import LLMError, LLMRequest, LLMResponse, Message, TextBlock, ToolResult, Usage
from app.tools.registry import ToolContext, ToolRegistry

TOOL_BUDGET_NOTE = {
    "zh": "工具调用次数已达上限,请基于已有信息直接给出最终答案。",
    "en": "The tool-call budget is used up. Give your final answer with the information you have.",
}


class LoopLimitError(LLMError):
    pass


class TurnTrace(BaseModel):
    endpoint: str
    model: str
    stop_reason: str
    latency_ms: int
    tokens_in: int
    tokens_out: int
    failover: bool
    tool_calls: list[dict] = Field(default_factory=list)


class LoopResult(BaseModel):
    response: LLMResponse
    messages: list[Message]
    turns: int
    tool_turns: int
    usage: Usage
    cost_usd: float | None
    failover: bool
    degraded: bool
    endpoints: list[str]
    trace: list[TurnTrace]
    latency_ms: int


async def run_tool_loop(
    gateway: LLMGateway,
    req: LLMRequest,
    *,
    tools: ToolRegistry,
    allowed: list[str],
    ctx: ToolContext,
    max_tool_turns: int,
) -> LoopResult:
    t0 = time.monotonic()
    specs = [t.spec() for t in tools.for_skill(allowed)]
    allowed_set = set(allowed)
    messages = list(req.messages)
    budget = max_tool_turns if specs else 0
    tool_turns = 0
    usage = Usage()
    cost: float | None = None
    trace: list[TurnTrace] = []
    failover = degraded = False
    endpoints: list[str] = []
    max_iterations = budget + 4  # head-room for pause_turn continuations

    for _ in range(max_iterations):
        can_use_tools = bool(specs) and tool_turns < budget
        turn_req = req.model_copy(
            update={
                "messages": list(messages),
                "tools": specs,
                "tool_choice": "auto" if can_use_tools else "none",
            }
        )
        resp = await gateway.complete(turn_req)
        usage = usage + resp.usage
        if resp.cost_usd is not None:
            cost = (cost or 0.0) + resp.cost_usd
        failover |= resp.failover
        degraded |= resp.degraded
        if resp.endpoint not in endpoints:
            endpoints.append(resp.endpoint)
        ep_cap = gateway.endpoint(resp.endpoint).max_tool_turns
        if ep_cap is not None:
            budget = min(budget, ep_cap)
        tt = TurnTrace(
            endpoint=resp.endpoint,
            model=resp.model,
            stop_reason=resp.stop_reason,
            latency_ms=resp.latency_ms,
            tokens_in=resp.usage.input_tokens,
            tokens_out=resp.usage.output_tokens,
            failover=resp.failover,
        )
        trace.append(tt)
        messages.append(resp.message)

        if resp.stop_reason == "pause_turn":
            continue
        calls = resp.tool_calls
        if resp.stop_reason != "tool_use" or not calls:
            return LoopResult(
                response=resp,
                messages=messages,
                turns=len(trace),
                tool_turns=tool_turns,
                usage=usage,
                cost_usd=cost,
                failover=failover,
                degraded=degraded,
                endpoints=endpoints,
                trace=trace,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

        tool_turns += 1
        if tool_turns > budget:
            # The model called tools although none were allowed any more: answer with errors.
            results = [tools_error(c.id) for c in calls]
        else:
            results = list(await asyncio.gather(*(tools.execute(c, ctx, allowed_set) for c in calls)))
        for c, r in zip(calls, results, strict=True):
            tt.tool_calls.append(
                {"name": c.name, "input": c.input, "is_error": r.is_error, "chars": len(r.content)}
            )
        content: list = list(results)
        if tool_turns >= budget:
            content.append(TextBlock(text=TOOL_BUDGET_NOTE.get(ctx.lang, TOOL_BUDGET_NOTE["en"])))
        messages.append(Message(role="user", content=content))

    raise LoopLimitError(f"tool loop exceeded {max_iterations} model calls")


def tools_error(call_id: str) -> ToolResult:
    return ToolResult(tool_call_id=call_id, content="Error: tool budget exhausted", is_error=True)
