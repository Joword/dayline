from __future__ import annotations

import json

import httpx
import pytest

from app.llm.adapters.openai_compat import OpenAICompatAdapter
from app.llm.endpoints import EndpointConfig
from app.llm.types import (
    EndpointLLMError,
    FatalLLMError,
    LLMRequest,
    Message,
    NativeContent,
    RetryableLLMError,
    SystemPart,
    TextBlock,
    ToolCall,
    ToolResult,
    ToolSpec,
)

OK = {
    "model": "m-large",
    "choices": [
        {
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "hello",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": json.dumps({"q": "x"})},
                    }
                ],
            },
        }
    ],
    "usage": {
        "prompt_tokens": 12,
        "completion_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 5},
    },
}


class Recorder:
    def __init__(
        self,
        status: int = 200,
        body: dict | None = None,
        headers: dict | None = None,
        raw: bytes | None = None,
    ):
        self.status = status
        self.body = OK if body is None else body
        self.headers = headers or {}
        self.raw = raw
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raw is not None:
            return httpx.Response(self.status, content=self.raw, headers=self.headers)
        return httpx.Response(self.status, json=self.body, headers=self.headers)

    @property
    def last_json(self) -> dict:
        return json.loads(self.requests[-1].content)


def ep(**kw) -> EndpointConfig:
    base = {
        "name": "o",
        "adapter": "openai_compat",
        "base_url": "http://llm.test/v1",
        "api_key_env": "TEST_KEY",
        "models": {"large": "m-large"},
        "features": {
            "tool_calling": True,
            "json_schema": True,
            "streaming": True,
            "prompt_caching": False,
            "effort": True,
        },
    }
    base.update(kw)
    return EndpointConfig.model_validate(base)


async def call(rec: Recorder, endpoint: EndpointConfig, request: LLMRequest):
    adapter = OpenAICompatAdapter(transport=httpx.MockTransport(rec))
    try:
        return await adapter.complete(request, endpoint, "m-large")
    finally:
        await adapter.aclose()


async def test_request_shape_and_response_parsing(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    rec = Recorder()
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    history = Message(
        role="assistant",
        content=[ToolCall(id="call_0", name="lookup", input={"q": "old"})],
        native=NativeContent(
            adapter="openai_compat",
            blocks=[{"role": "assistant", "content": None, "tool_calls": [{"id": "call_0"}]}],
        ),
    )
    request = LLMRequest(
        system=[SystemPart(text="core"), SystemPart(text="skill")],
        messages=[
            history,
            Message(
                role="user",
                content=[ToolResult(tool_call_id="call_0", content="found"), TextBlock(text="go on")],
            ),
            Message.user("hi"),
        ],
        tools=[ToolSpec(name="lookup", description="d", input_schema={"type": "object", "properties": {}})],
        output_schema=schema,
        effort="low",
        max_tokens=1000,
    )
    out = await call(rec, ep(), request)
    body = rec.last_json
    assert rec.requests[-1].url.path == "/v1/chat/completions"
    assert rec.requests[-1].headers["authorization"] == "Bearer sk-test"
    assert body["model"] == "m-large" and body["max_tokens"] == 1000
    assert body["messages"][0] == {"role": "system", "content": "core\n\nskill"}
    assert body["messages"][1]["tool_calls"] == [{"id": "call_0"}]
    assert body["messages"][2] == {"role": "tool", "tool_call_id": "call_0", "content": "found"}
    assert body["messages"][3] == {"role": "user", "content": "go on"}
    assert body["response_format"]["json_schema"]["schema"] == schema
    assert body["reasoning_effort"] == "low"
    assert "tool_choice" not in body
    assert out.text == "hello" and out.stop_reason == "tool_use"
    assert out.tool_calls[0].input == {"q": "x"}
    assert out.usage.input_tokens == 12 and out.usage.cache_read_tokens == 5
    assert out.message.native is not None and out.message.native.adapter == "openai_compat"


async def test_schema_falls_back_to_the_prompt_and_tool_choice_none(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    rec = Recorder(
        body={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}]}
    )
    schema = {"type": "object"}
    request = LLMRequest(
        messages=[Message.user("hi")],
        tools=[ToolSpec(name="lookup", description="d", input_schema={"type": "object"})],
        tool_choice="none",
        output_schema=schema,
    )
    await call(rec, ep(features={"json_schema": False, "tool_calling": True, "effort": False}), request)
    body = rec.last_json
    assert "response_format" not in body and "reasoning_effort" not in body
    assert schema["type"] in body["messages"][0]["content"]
    assert body["tool_choice"] == "none"


async def test_status_and_transport_errors(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    rec = Recorder(status=429, body={"error": {"message": "slow"}}, headers={"retry-after": "2"})
    with pytest.raises(RetryableLLMError) as ei:
        await call(rec, ep(), LLMRequest(messages=[Message.user("hi")]))
    assert ei.value.retry_after == 2.0

    rec400 = Recorder(status=400, body={"error": {"message": "bad schema"}})
    with pytest.raises(FatalLLMError):
        await call(rec400, ep(), LLMRequest(messages=[Message.user("hi")]))

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    adapter = OpenAICompatAdapter(transport=httpx.MockTransport(boom))
    with pytest.raises(RetryableLLMError):
        await adapter.complete(LLMRequest(messages=[Message.user("hi")]), ep(), "m-large")
    await adapter.aclose()


async def test_stream_assembles_text_and_tool_calls(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    chunks = [
        {"choices": [{"delta": {"content": "he"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "llo"}, "finish_reason": None}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call_1", "function": {"name": "lookup", "arguments": '{"q":'}}
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "model": "m-served",
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}]},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3},
        },
    ]
    raw = "".join(f"data: {json.dumps(c)}\n" for c in chunks).encode() + b"data: [DONE]\n"
    rec = Recorder(raw=raw)
    adapter = OpenAICompatAdapter(transport=httpx.MockTransport(rec))
    pieces = []
    done = None
    async for event in adapter.stream(LLMRequest(messages=[Message.user("hi")]), ep(), "m-large"):
        if hasattr(event, "text"):
            pieces.append(event.text)
        else:
            done = event.response
    await adapter.aclose()
    assert pieces == ["he", "llo"]
    assert done is not None
    assert done.text == "hello" and done.model == "m-served" and done.stop_reason == "tool_use"
    assert done.tool_calls[0].input == {"q": "x"}
    assert done.usage.output_tokens == 3


def test_missing_key_is_an_endpoint_error(monkeypatch):
    monkeypatch.delenv("TEST_KEY", raising=False)
    adapter = OpenAICompatAdapter()
    with pytest.raises(EndpointLLMError):
        adapter._client(ep())
