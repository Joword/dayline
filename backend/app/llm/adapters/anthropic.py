"""Anthropic Messages API adapter.

Covers Anthropic direct, Anthropic via an HTTP proxy, and any gateway that speaks
the Anthropic-compatible API (``base_url`` + ``auth_header``). The SDK's own
retries are disabled (``max_retries=0``): the gateway decides when to retry and
when to fail over.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from app.llm.adapters.base import JSON_INSTRUCTION, AdapterDone
from app.llm.endpoints import EndpointConfig
from app.llm.types import (
    EndpointLLMError,
    FatalLLMError,
    LLMError,
    LLMRequest,
    LLMResponse,
    Message,
    NativeContent,
    RetryableLLMError,
    TextBlock,
    TextDelta,
    ToolCall,
    ToolResult,
    Usage,
)

SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"
_STOP_REASONS = {"end_turn", "tool_use", "max_tokens", "refusal", "stop_sequence", "pause_turn"}


def _retry_after(e: anthropic.APIStatusError) -> float | None:
    try:
        v = e.response.headers.get("retry-after")
        return float(v) if v is not None else None
    except (ValueError, AttributeError):
        return None


def map_error(e: Exception) -> LLMError:
    if isinstance(e, anthropic.APIConnectionError):  # includes APITimeoutError
        return RetryableLLMError(f"{type(e).__name__}: {e}")
    if isinstance(e, anthropic.APIStatusError):
        s = e.status_code
        msg = f"HTTP {s} {type(e).__name__}: {getattr(e, 'message', e)}"
        if s in (408, 409, 429) or s >= 500:
            return RetryableLLMError(msg, retry_after=_retry_after(e))
        if s in (401, 403, 404):
            return EndpointLLMError(msg)
        return FatalLLMError(msg)
    if isinstance(e, anthropic.CredentialsError):
        return EndpointLLMError(f"credentials: {e}")
    if isinstance(e, anthropic.AnthropicError):
        return EndpointLLMError(f"{type(e).__name__}: {e}")
    return FatalLLMError(f"{type(e).__name__}: {e}")


def _to_api_message(m: Message) -> dict[str, Any]:
    if m.native is not None and m.native.adapter == "anthropic":
        # Replay the provider's own blocks verbatim (thinking signatures, fallback markers).
        return {"role": m.role, "content": m.native.blocks}
    blocks: list[dict[str, Any]] = []
    for b in m.content:
        if isinstance(b, TextBlock):
            if b.text:
                blocks.append({"type": "text", "text": b.text})
        elif isinstance(b, ToolCall):
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif isinstance(b, ToolResult):
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": b.tool_call_id,
                    "content": b.content,
                    "is_error": b.is_error,
                }
            )
    return {"role": m.role, "content": blocks}


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, transport: Any = None):
        # `transport` lets tests inject an httpx2.MockTransport.
        self._transport = transport
        self._clients: dict[str, anthropic.AsyncAnthropic] = {}

    # --- client -------------------------------------------------------------

    def _client(self, ep: EndpointConfig) -> anthropic.AsyncAnthropic:
        if ep.name in self._clients:
            return self._clients[ep.name]
        key = ep.api_key()
        if not key:
            raise EndpointLLMError(f"env var {ep.api_key_env} is not set")
        # Always explicit: an inherited ANTHROPIC_BASE_URL must not silently redirect an
        # endpoint - llm_endpoints.yaml is the only place that decides where requests go.
        kwargs: dict[str, Any] = {
            "api_key": key,
            "base_url": ep.base_url or DEFAULT_BASE_URL,
            "max_retries": 0,
            "timeout": ep.timeout_s,
        }
        if ep.auth_header == "x-api-key":
            kwargs["default_headers"] = {"X-Api-Key": key, "Authorization": anthropic.Omit()}
        http: dict[str, Any] = {}
        if ep.proxy == "none":
            http["trust_env"] = False
        elif ep.proxy != "env":
            http["proxy"] = ep.proxy
        if self._transport is not None:
            http["transport"] = self._transport
        if http:
            kwargs["http_client"] = anthropic.DefaultAsyncHttpxClient(**http)
        client = anthropic.AsyncAnthropic(**kwargs)
        self._clients[ep.name] = client
        return client

    async def aclose(self) -> None:
        for c in self._clients.values():
            await c.close()
        self._clients.clear()

    # --- request / response ---------------------------------------------------

    def build_params(self, req: LLMRequest, ep: EndpointConfig, model: str) -> tuple[dict[str, Any], bool]:
        parts = req.system_for(ep.prompt_variant)
        system: list[dict[str, Any]] = []
        last_cacheable = max((i for i, p in enumerate(parts) if p.cacheable), default=None)
        for i, p in enumerate(parts):
            block: dict[str, Any] = {"type": "text", "text": p.text}
            if ep.features.prompt_caching and i == last_cacheable:
                block["cache_control"] = {"type": "ephemeral"}
            system.append(block)

        output_config: dict[str, Any] = {}
        if req.output_schema is not None:
            if ep.features.json_schema:
                schema = anthropic.transform_schema(copy.deepcopy(req.output_schema))
                output_config["format"] = {"type": "json_schema", "schema": schema}
            else:
                # No constrained decoding on this endpoint: ask for JSON in the prompt;
                # the runner validates and retries either way.
                system.append(
                    {
                        "type": "text",
                        "text": JSON_INSTRUCTION + json.dumps(req.output_schema, ensure_ascii=False),
                    }
                )
        if req.effort and ep.features.effort:
            output_config["effort"] = req.effort

        params: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            "messages": [_to_api_message(m) for m in req.messages],
        }
        if system:
            params["system"] = system
        if req.tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in req.tools
            ]
            if req.tool_choice == "none":
                params["tool_choice"] = {"type": "none"}
        if output_config:
            params["output_config"] = output_config
        use_beta = model in ep.server_fallback_models
        if use_beta:
            params["betas"] = [SERVER_FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params, use_beta

    def parse_response(self, resp: Any, latency_ms: int) -> LLMResponse:
        content: list[TextBlock | ToolCall] = []
        for b in resp.content:
            if b.type == "text":
                content.append(TextBlock(text=b.text))
            elif b.type == "tool_use":
                content.append(ToolCall(id=b.id, name=b.name, input=dict(b.input or {})))
        native = NativeContent(adapter=self.name, blocks=[b.to_dict() for b in resp.content])
        stop = resp.stop_reason if resp.stop_reason in _STOP_REASONS else "other"
        u = resp.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
        )
        category = None
        if stop == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None) if details else None
        return LLMResponse(
            message=Message(role="assistant", content=content, native=native),
            stop_reason=stop,
            usage=usage,
            model=resp.model,
            refusal_category=category,
            latency_ms=latency_ms,
        )

    # --- calls ----------------------------------------------------------------

    async def complete(self, req: LLMRequest, ep: EndpointConfig, model: str) -> LLMResponse:
        client = self._client(ep)
        params, beta = self.build_params(req, ep, model)
        api = client.beta.messages if beta else client.messages
        start = time.monotonic()
        try:
            resp = await api.create(**params)
        except Exception as e:
            raise map_error(e) from e
        return self.parse_response(resp, int((time.monotonic() - start) * 1000))

    async def stream(
        self, req: LLMRequest, ep: EndpointConfig, model: str
    ) -> AsyncIterator[TextDelta | AdapterDone]:
        client = self._client(ep)
        params, beta = self.build_params(req, ep, model)
        api = client.beta.messages if beta else client.messages
        start = time.monotonic()
        try:
            async with api.stream(**params) as s:
                async for ev in s:
                    if ev.type == "text":
                        yield TextDelta(text=ev.text)
                final = await s.get_final_message()
        except LLMError:
            raise
        except Exception as e:
            raise map_error(e) from e
        yield AdapterDone(self.parse_response(final, int((time.monotonic() - start) * 1000)))
