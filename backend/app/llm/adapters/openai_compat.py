"""OpenAI-compatible Chat Completions adapter.

Speaks the subset that local servers and corporate gateways usually implement:
``/chat/completions``, tools, JSON schema via ``response_format``, and SSE streaming.
The API key stays in the environment named by the endpoint. No vendor SDK.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

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

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_STOP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}
_EFFORT = {"low", "medium", "high"}


def map_status(status: int, body: bytes, headers: httpx.Headers) -> LLMError:
    detail = ""
    try:
        err = json.loads(body).get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or "")
        elif isinstance(err, str):
            detail = err
    except (json.JSONDecodeError, AttributeError):
        detail = body.decode("utf-8", errors="replace")[:300]
    msg = f"HTTP {status} {detail}".strip()
    if status in (408, 409, 429) or status >= 500:
        retry_after = None
        try:
            if (raw := headers.get("retry-after")) is not None:
                retry_after = float(raw)
        except ValueError:
            retry_after = None
        return RetryableLLMError(msg, retry_after)
    if status in (401, 403, 404):
        return EndpointLLMError(msg)
    return FatalLLMError(msg)


def map_error(e: Exception) -> LLMError:
    if isinstance(e, LLMError):
        return e
    if isinstance(e, httpx.TransportError):  # connect, read, timeout
        return RetryableLLMError(f"{type(e).__name__}: {e}")
    if isinstance(e, httpx.HTTPStatusError):
        return map_status(e.response.status_code, e.response.content, e.response.headers)
    return FatalLLMError(f"{type(e).__name__}: {e}")


def _loads_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _to_messages(req: LLMRequest, ep: EndpointConfig) -> list[dict[str, Any]]:
    parts = [p.text for p in req.system_for(ep.prompt_variant) if p.text]
    if req.output_schema is not None and not ep.features.json_schema:
        parts.append(JSON_INSTRUCTION + json.dumps(req.output_schema, ensure_ascii=False))
    out: list[dict[str, Any]] = []
    if parts:
        out.append({"role": "system", "content": "\n\n".join(parts)})
    for message in req.messages:
        out.extend(_to_api_messages(message))
    return out


def _to_api_messages(message: Message) -> list[dict[str, Any]]:
    if message.native is not None and message.native.adapter == "openai_compat" and message.native.blocks:
        return [dict(message.native.blocks[0])]
    tool_results = [b for b in message.content if isinstance(b, ToolResult)]
    texts = [b.text for b in message.content if isinstance(b, TextBlock) and b.text]
    calls = [b for b in message.content if isinstance(b, ToolCall)]
    if tool_results:
        out = [{"role": "tool", "tool_call_id": b.tool_call_id, "content": b.content} for b in tool_results]
        if texts:
            out.append({"role": "user", "content": "\n\n".join(texts)})
        return out
    if message.role == "assistant":
        body: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
        if calls:
            body["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.input, ensure_ascii=False)},
                }
                for c in calls
            ]
        return [body]
    return [{"role": "user", "content": "\n".join(texts)}]


class OpenAICompatAdapter:
    name = "openai_compat"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport
        self._clients: dict[str, httpx.AsyncClient] = {}

    def _client(self, ep: EndpointConfig) -> httpx.AsyncClient:
        if ep.name in self._clients:
            return self._clients[ep.name]
        key = ep.api_key()
        if not key:
            raise EndpointLLMError(f"env var {ep.api_key_env} is not set")
        headers = {"X-Api-Key": key} if ep.auth_header == "x-api-key" else {"Authorization": f"Bearer {key}"}
        kwargs: dict[str, Any] = {
            "base_url": (ep.base_url or DEFAULT_BASE_URL).rstrip("/"),
            "headers": headers,
            "timeout": ep.timeout_s,
            "trust_env": ep.proxy == "env",
        }
        if ep.proxy not in ("env", "none"):
            kwargs["proxy"] = ep.proxy
        if self._transport is not None:
            kwargs["transport"] = self._transport
        client = httpx.AsyncClient(**kwargs)
        self._clients[ep.name] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    def build_payload(self, req: LLMRequest, ep: EndpointConfig, model: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            "messages": _to_messages(req, ep),
        }
        if req.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in req.tools
            ]
            if req.tool_choice == "none":
                payload["tool_choice"] = "none"
        if req.output_schema is not None and ep.features.json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "dayline_output", "schema": req.output_schema, "strict": False},
            }
        if req.effort in _EFFORT and ep.features.effort:
            payload["reasoning_effort"] = req.effort
        return payload

    def parse_message(
        self, message: dict[str, Any], *, model: str, finish: str | None, usage: dict | None, latency_ms: int
    ) -> LLMResponse:
        content: list[TextBlock | ToolCall] = []
        if text := message.get("content"):
            if isinstance(text, str) and text:
                content.append(TextBlock(text=text))
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            content.append(
                ToolCall(
                    id=str(call.get("id") or ""),
                    name=str(fn.get("name") or ""),
                    input=_loads_args(fn.get("arguments")),
                )
            )
        stop = _STOP.get(finish or "", "other")
        if any(isinstance(b, ToolCall) for b in content) and stop == "end_turn":
            stop = "tool_use"
        u = usage or {}
        details = u.get("prompt_tokens_details") or {}
        return LLMResponse(
            message=Message(
                role="assistant",
                content=content,
                native=NativeContent(adapter=self.name, blocks=[message]),
            ),
            stop_reason=stop,
            usage=Usage(
                input_tokens=int(u.get("prompt_tokens") or 0),
                output_tokens=int(u.get("completion_tokens") or 0),
                cache_read_tokens=int(details.get("cached_tokens") or 0),
            ),
            model=model,
            refusal_category="content_filter" if stop == "refusal" else None,
            latency_ms=latency_ms,
        )

    async def _post(self, ep: EndpointConfig, payload: dict[str, Any]) -> httpx.Response:
        try:
            resp = await self._client(ep).post("/chat/completions", json=payload)
        except Exception as e:
            raise map_error(e) from e
        if resp.status_code >= 400:
            raise map_status(resp.status_code, resp.content, resp.headers)
        return resp

    async def complete(self, req: LLMRequest, ep: EndpointConfig, model: str) -> LLMResponse:
        start = time.monotonic()
        resp = await self._post(ep, self.build_payload(req, ep, model))
        try:
            body = resp.json()
        except json.JSONDecodeError as e:
            raise FatalLLMError("response was not JSON") from e
        choice = (body.get("choices") or [{}])[0]
        return self.parse_message(
            choice.get("message") or {},
            model=str(body.get("model") or model),
            finish=choice.get("finish_reason"),
            usage=body.get("usage"),
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    async def stream(
        self, req: LLMRequest, ep: EndpointConfig, model: str
    ) -> AsyncIterator[TextDelta | AdapterDone]:
        payload = self.build_payload(req, ep, model)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        start = time.monotonic()
        text = ""
        calls: dict[int, dict[str, str]] = {}
        finish: str | None = None
        usage: dict | None = None
        served = model
        try:
            async with self._client(ep).stream("POST", "/chat/completions", json=payload) as resp:
                if resp.status_code >= 400:
                    raise map_status(resp.status_code, await resp.aread(), resp.headers)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as e:
                        raise FatalLLMError("stream chunk was not JSON") from e
                    served = str(chunk.get("model") or served)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choice = (chunk.get("choices") or [{}])[0]
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    if piece := delta.get("content"):
                        text += piece
                        yield TextDelta(text=piece)
                    for call in delta.get("tool_calls") or []:
                        slot = calls.setdefault(
                            int(call.get("index") or 0), {"id": "", "name": "", "arguments": ""}
                        )
                        if call.get("id"):
                            slot["id"] = call["id"]
                        fn = call.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
        except LLMError:
            raise
        except Exception as e:
            raise map_error(e) from e
        message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            message["tool_calls"] = [
                {
                    "id": slot["id"],
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": slot["arguments"]},
                }
                for _, slot in sorted(calls.items())
            ]
        yield AdapterDone(
            self.parse_message(
                message,
                model=served,
                finish=finish,
                usage=usage,
                latency_ms=int((time.monotonic() - start) * 1000),
            )
        )
