from __future__ import annotations

import json

import httpx2
import pytest

from app.llm.adapters.anthropic import SERVER_FALLBACK_BETA, AnthropicAdapter
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

OK_BODY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "m-large",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig-abc"},
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 12,
        "output_tokens": 7,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 0,
    },
}


class Recorder:
    def __init__(self, status: int = 200, body: dict | None = None, headers: dict | None = None):
        self.status, self.body, self.headers = status, body or OK_BODY, headers or {}
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, json=self.body, headers=self.headers)

    @property
    def last_json(self) -> dict:
        return json.loads(self.requests[-1].content)


def ep(**kw) -> EndpointConfig:
    base = {
        "name": "t",
        "base_url": "http://llm.test",
        "api_key_env": "TEST_KEY",
        "models": {"large": "m-large"},
        "features": {
            "tool_calling": True,
            "json_schema": True,
            "streaming": True,
            "prompt_caching": True,
            "effort": True,
        },
    }
    base.update(kw)
    return EndpointConfig.model_validate(base)


async def call(rec: Recorder, endpoint: EndpointConfig, request: LLMRequest):
    adapter = AnthropicAdapter(transport=httpx2.MockTransport(rec))
    try:
        return await adapter.complete(request, endpoint, "m-large")
    finally:
        await adapter.aclose()


async def test_request_shape_and_response_parsing():
    rec = Recorder()
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    r = LLMRequest(
        system=[SystemPart(text="core"), SystemPart(text="skill", cacheable=True), SystemPart(text="tail")],
        messages=[Message.user("hi")],
        tools=[ToolSpec(name="lookup", description="d", input_schema={"type": "object", "properties": {}})],
        output_schema=schema,
        effort="low",
        max_tokens=1000,
    )
    out = await call(rec, ep(), r)
    body = rec.last_json
    assert body["model"] == "m-large" and body["max_tokens"] == 1000
    assert [b.get("cache_control") for b in body["system"]] == [None, {"type": "ephemeral"}, None]
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert "tool_choice" not in body and "temperature" not in body
    assert rec.requests[-1].headers["authorization"] == "Bearer sk-test"

    assert out.text == "hello" and out.stop_reason == "tool_use"
    assert out.tool_calls[0].input == {"q": "x"}
    assert out.usage.cache_read_tokens == 5
    # provider blocks (incl. the thinking signature) are kept for verbatim replay
    assert out.message.native.blocks[0] == {"type": "thinking", "thinking": "", "signature": "sig-abc"}


async def test_features_off_means_no_caching_effort_or_constrained_output():
    rec = Recorder()
    e = ep(features={"tool_calling": True})
    r = LLMRequest(
        system=[SystemPart(text="s", cacheable=True)],
        messages=[Message.user("hi")],
        output_schema={"type": "object"},
        effort="high",
    )
    await call(rec, e, r)
    body = rec.last_json
    assert "output_config" not in body
    assert "cache_control" not in body["system"][0]
    assert "JSON Schema" in body["system"][-1]["text"]


async def test_native_blocks_replayed_verbatim_and_neutral_blocks_converted():
    rec = Recorder()
    native = NativeContent(adapter="anthropic", blocks=OK_BODY["content"])
    history = [
        Message.user("hi"),
        Message(role="assistant", content=[TextBlock(text="hello")], native=native),
        Message(role="user", content=[ToolResult(tool_call_id="toolu_1", content="42")]),
        Message(role="assistant", content=[ToolCall(id="toolu_2", name="lookup", input={})]),
    ]
    await call(
        rec,
        ep(),
        LLMRequest(
            messages=history,
            tool_choice="none",
            tools=[ToolSpec(name="lookup", description="d", input_schema={"type": "object"})],
        ),
    )
    body = rec.last_json
    assert body["messages"][1]["content"] == OK_BODY["content"]
    assert body["messages"][2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "42",
        "is_error": False,
    }
    assert body["messages"][3]["content"][0]["type"] == "tool_use"
    assert body["tool_choice"] == {"type": "none"}


async def test_x_api_key_mode_sends_only_that_header():
    rec = Recorder()
    await call(rec, ep(auth_header="x-api-key"), LLMRequest(messages=[Message.user("hi")]))
    h = rec.requests[-1].headers
    assert h["x-api-key"] == "sk-test" and "authorization" not in h


async def test_server_side_fallback_uses_beta_for_listed_models():
    rec = Recorder()
    await call(rec, ep(server_fallback_models=["m-large"]), LLMRequest(messages=[Message.user("hi")]))
    assert rec.last_json["fallbacks"] == "default"
    assert SERVER_FALLBACK_BETA in rec.requests[-1].headers["anthropic-beta"]


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (500, RetryableLLMError),
        (529, RetryableLLMError),
        (429, RetryableLLMError),
        (400, FatalLLMError),
        (401, EndpointLLMError),
        (404, EndpointLLMError),
    ],
)
async def test_error_mapping(status, exc):
    rec = Recorder(
        status=status,
        body={"type": "error", "error": {"type": "x", "message": "boom"}},
        headers={"retry-after": "2"},
    )
    with pytest.raises(exc) as ei:
        await call(rec, ep(), LLMRequest(messages=[Message.user("hi")]))
    if status == 429:
        assert ei.value.retry_after == 2.0
    assert len(rec.requests) == 1  # SDK retries are off; the gateway owns retrying


async def test_connection_error_is_retryable():
    def boom(request):
        raise httpx2.ConnectError("refused")

    adapter = AnthropicAdapter(transport=httpx2.MockTransport(boom))
    with pytest.raises(RetryableLLMError):
        await adapter.complete(LLMRequest(messages=[Message.user("hi")]), ep(), "m-large")
    await adapter.aclose()


async def test_refusal_category_is_parsed():
    body = {
        **OK_BODY,
        "content": [],
        "stop_reason": "refusal",
        "stop_details": {"type": "refusal", "category": "cyber", "explanation": "x"},
    }
    out = await call(Recorder(body=body), ep(), LLMRequest(messages=[Message.user("hi")]))
    assert out.stop_reason == "refusal" and out.refusal_category == "cyber"


async def test_inherited_sdk_env_vars_do_not_redirect_or_leak(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://somewhere-else.test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "inherited-token")
    for mode in ("bearer", "x-api-key"):
        rec = Recorder()
        await call(rec, ep(base_url=None, auth_header=mode), LLMRequest(messages=[Message.user("hi")]))
        sent = rec.requests[-1]
        assert sent.url.host == "api.anthropic.com"
        assert "inherited-token" not in str(dict(sent.headers))
