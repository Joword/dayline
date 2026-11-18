"""Vendor-neutral message format used by the SkillRunner, tool loop and gateway.

Nothing outside ``app/llm/adapters`` knows any provider's request shape. The one
concession is ``Message.native``: the exact provider blocks of an assistant turn
(thinking blocks, fallback markers, ...) kept so the turn can be replayed
verbatim in a tool loop. Adapters use ``native`` only when it came from the same
adapter type; otherwise they rebuild the turn from the neutral blocks.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

Tier = Literal["small", "large"]
DataLevel = Literal["L1", "L2", "L3"]  # L1 org/project, L2 team summary, L3 personal detail
Effort = Literal["low", "medium", "high", "xhigh", "max"]
StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "stop_sequence", "pause_turn", "other"]

DATA_LEVEL_RANK: dict[str, int] = {"L1": 1, "L2": 2, "L3": 3}


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolCall(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    content: str
    is_error: bool = False


ContentBlock = Annotated[TextBlock | ToolCall | ToolResult, Field(discriminator="type")]


class NativeContent(BaseModel):
    adapter: str
    blocks: list[dict[str, Any]]


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: list[ContentBlock]
    native: NativeContent | None = None

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role="user", content=[TextBlock(text=text)])

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [b for b in self.content if isinstance(b, ToolCall)]


class SystemPart(BaseModel):
    text: str
    # Marks the end of the stable prefix. Adapters with prompt caching put the
    # cache breakpoint on the last cacheable part; others ignore it.
    cacheable: bool = False


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class LLMRequest(BaseModel):
    tier: Tier = "large"
    system: list[SystemPart] = Field(default_factory=list)
    # Per-endpoint prompt overrides, keyed by an endpoint's `prompt_variant`
    # (e.g. "corp" -> prompt.zh.corp.md).
    system_variants: dict[str, list[SystemPart]] = Field(default_factory=dict)
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    # "none" keeps the tool definitions (history may hold tool calls) but forbids new calls.
    tool_choice: Literal["auto", "none"] = "auto"
    output_schema: dict[str, Any] | None = None  # JSON Schema the final answer must follow
    max_tokens: int = 16000
    effort: Effort | None = None
    data_level: DataLevel = "L3"
    fallback: Literal["allow", "deny"] = "allow"
    purpose: str = ""  # for logs/metrics, e.g. the skill name

    def system_for(self, variant: str | None) -> list[SystemPart]:
        if variant and variant in self.system_variants:
            return self.system_variants[variant]
        return self.system


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


class Attempt(BaseModel):
    endpoint: str
    ok: bool
    error: str | None = None
    latency_ms: int = 0
    skipped: bool = False  # never sent: circuit open, data level, missing feature ...


class LLMResponse(BaseModel):
    message: Message
    stop_reason: StopReason
    usage: Usage = Field(default_factory=Usage)
    model: str
    endpoint: str = ""
    # served by an endpoint other than the primary one (order[0])
    failover: bool = False
    # served by a different model than the primary endpoint would have used
    degraded: bool = False
    refusal_category: str | None = None
    latency_ms: int = 0
    cost_usd: float | None = None
    attempts: list[Attempt] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def tool_calls(self) -> list[ToolCall]:
        return self.message.tool_calls


# --- streaming --------------------------------------------------------------


class TextDelta(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class StreamRestarted(BaseModel):
    """The stream broke mid-way; output so far is void and the request restarts elsewhere."""

    type: Literal["restarted"] = "restarted"
    endpoint: str
    reason: str


class StreamDone(BaseModel):
    type: Literal["done"] = "done"
    response: LLMResponse


StreamEvent = TextDelta | StreamRestarted | StreamDone


# --- errors -----------------------------------------------------------------


class LLMError(Exception):
    pass


class RetryableLLMError(LLMError):
    """Transient: connection failure, timeout, 408/409/429/5xx. Retry, then fail over."""

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class EndpointLLMError(LLMError):
    """The endpoint itself is unusable for us (401/403/404: bad key, model missing).

    Not worth retrying on the same endpoint, but another endpoint may work.
    """


class FatalLLMError(LLMError):
    """The request itself is wrong (400/413/422 ...). Switching endpoints won't help."""


class RefusalError(LLMError):
    def __init__(self, category: str | None, response: LLMResponse | None = None):
        super().__init__(f"model declined the request (category={category})")
        self.category = category
        self.response = response


class NoEndpointAvailable(LLMError):
    """Every endpoint was skipped or failed. ``queued`` hints the caller may retry later."""

    def __init__(self, attempts: list[Attempt], queued: bool = False):
        lines = [f"{a.endpoint}: {'skipped - ' if a.skipped else ''}{a.error}" for a in attempts]
        super().__init__(
            "no LLM endpoint could serve the request:\n  " + "\n  ".join(lines or ["(none configured)"])
        )
        self.attempts = attempts
        self.queued = queued
