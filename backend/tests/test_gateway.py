from __future__ import annotations

import pytest

from app.llm.types import (
    EndpointLLMError,
    FatalLLMError,
    NoEndpointAvailable,
    RefusalError,
    RetryableLLMError,
    StreamDone,
    StreamRestarted,
    TextDelta,
    ToolSpec,
)
from tests.conftest import FakeAdapter, FakeClock, make_config, make_gateway, req, resp


async def test_primary_serves_when_healthy():
    a = FakeAdapter({"e1": [resp("ok")]})
    r = await make_gateway(a).complete(req())
    assert (r.endpoint, r.failover, r.degraded, r.text) == ("e1", False, False, "ok")
    assert r.cost_usd == pytest.approx((100 * 2.0 + 50 * 10.0) / 1e6)


async def test_retryable_errors_retry_then_fail_over(sleeps):
    a = FakeAdapter({"e1": [RetryableLLMError("503")] * 3, "e2": [resp("from e2")]})
    r = await make_gateway(a, sleeps=sleeps).complete(req())
    assert r.endpoint == "e2" and r.failover
    assert [c[0] for c in a.calls] == ["e1", "e1", "e1", "e2"]
    assert len(sleeps) == 2  # backoff between the 3 attempts on e1
    assert [x.ok for x in r.attempts] == [False, False, False, True]


async def test_retry_after_header_is_honoured_up_to_cap(sleeps):
    a = FakeAdapter({"e1": [RetryableLLMError("429", retry_after=3.0), resp("ok")]})
    await make_gateway(a, sleeps=sleeps).complete(req())
    assert sleeps == [3.0]


async def test_request_errors_do_not_fail_over():
    a = FakeAdapter({"e1": [FatalLLMError("400 bad request")], "e2": [resp("never")]})
    with pytest.raises(FatalLLMError):
        await make_gateway(a).complete(req())
    assert [c[0] for c in a.calls] == ["e1"]


async def test_endpoint_errors_switch_without_retrying():
    a = FakeAdapter({"e1": [EndpointLLMError("401")], "e2": [resp("ok")]})
    gw = make_gateway(a)
    r = await gw.complete(req())
    assert r.endpoint == "e2"
    assert [c[0] for c in a.calls] == ["e1", "e2"]
    assert gw.breakers["e1"].is_open


async def test_circuit_opens_then_half_open_trial_recovers():
    clock = FakeClock()
    a = FakeAdapter(
        {
            "e1": [RetryableLLMError("down")] * 3 + [resp("back")],
            "e2": [resp("e2-1"), resp("e2-2")],
        }
    )
    gw = make_gateway(a, clock=clock)
    assert (await gw.complete(req())).endpoint == "e2"
    # circuit is open: e1 is skipped without a call
    r = await gw.complete(req())
    assert r.endpoint == "e2"
    assert r.attempts[0].skipped and r.attempts[0].error == "circuit open"
    # after the cooldown one trial goes to e1; success closes the breaker
    clock.t += 61
    r = await gw.complete(req())
    assert r.endpoint == "e1" and not r.failover
    assert gw.breakers["e1"].state == "closed"


async def test_failed_half_open_trial_reopens():
    clock = FakeClock()
    a = FakeAdapter({"e1": [RetryableLLMError("down")] * 4, "e2": [resp("a"), resp("b")]})
    gw = make_gateway(a, clock=clock)
    await gw.complete(req())
    clock.t += 61
    r = await gw.complete(req())
    assert r.endpoint == "e2"
    assert [c[0] for c in a.calls].count("e1") == 4  # 3 + one trial, no retries in half-open
    assert gw.breakers["e1"].is_open


async def test_data_level_skips_and_signals_queueing():
    cfg = make_config("e1", "e2", e1={"max_data_level": "L2"}, e2={"max_data_level": "L1"})
    a = FakeAdapter()
    with pytest.raises(NoEndpointAvailable) as ei:
        await make_gateway(a, cfg).complete(req(data_level="L3"))
    assert ei.value.queued
    assert a.calls == []


async def test_data_level_skip_falls_through_to_capable_endpoint():
    cfg = make_config("e1", "e2", e1={"max_data_level": "L2"})
    a = FakeAdapter({"e2": [resp("ok")]})
    r = await make_gateway(a, cfg).complete(req(data_level="L3"))
    assert r.endpoint == "e2"
    assert "data level" in r.attempts[0].error


async def test_fallback_deny_uses_primary_only():
    a = FakeAdapter({"e1": [RetryableLLMError("x")] * 3})
    with pytest.raises(NoEndpointAvailable):
        await make_gateway(a).complete(req(fallback="deny"))
    assert {c[0] for c in a.calls} == {"e1"}


async def test_unconfigured_endpoints_are_skipped(monkeypatch):
    cfg = make_config(
        "e1", "e2", "e3", e1={"api_key_env": "MISSING_KEY"}, e2={"models": {"small": "TODO", "large": "TODO"}}
    )
    a = FakeAdapter({"e3": [resp("ok")]})
    r = await make_gateway(a, cfg).complete(req())
    assert r.endpoint == "e3"
    reasons = [x.error for x in r.attempts if x.skipped]
    assert "MISSING_KEY" in reasons[0] and "TODO" in reasons[1]


async def test_tool_requests_skip_endpoints_without_tool_calling():
    cfg = make_config("e1", "e2", e1={"features": {"tool_calling": "TODO"}})
    a = FakeAdapter({"e2": [resp("ok")]})
    tool = ToolSpec(name="t", description="d", input_schema={"type": "object"})
    r = await make_gateway(a, cfg).complete(req(tools=[tool]))
    assert r.endpoint == "e2"


async def test_context_limit_skips_small_endpoints():
    cfg = make_config("e1", "e2", e1={"max_context": 1000})
    a = FakeAdapter({"e2": [resp("ok")]})
    r = await make_gateway(a, cfg).complete(req(max_tokens=4000))
    assert r.endpoint == "e2" and "max_context" in r.attempts[0].error


async def test_degraded_when_backup_uses_other_model():
    cfg = make_config("e1", "e2", e2={"models": {"large": "other-model"}})
    a = FakeAdapter({"e1": [EndpointLLMError("403")], "e2": [resp("ok", model="other-model")]})
    r = await make_gateway(a, cfg).complete(req())
    assert r.failover and r.degraded


async def test_prompt_variant_is_chosen_per_endpoint():
    from app.llm.types import SystemPart

    cfg = make_config("e1", "e2", e2={"prompt_variant": "corp"})
    seen = []
    a = FakeAdapter({"e1": [EndpointLLMError("401")], "e2": [lambda r: (seen.append(r), resp("ok"))[1]]})
    gw = make_gateway(a, cfg)
    r = req(system=[SystemPart(text="default")], system_variants={"corp": [SystemPart(text="corp")]})
    await gw.complete(r)
    assert gw.endpoint("e2").prompt_variant == "corp"
    assert seen[0].system_for("corp")[0].text == "corp"


async def test_refusal_raises_with_response():
    a = FakeAdapter({"e1": [resp("", stop="refusal")]})
    with pytest.raises(RefusalError) as ei:
        await make_gateway(a).complete(req())
    assert ei.value.response is not None


async def test_stream_restarts_on_next_endpoint_after_partial_output():
    a = FakeAdapter(
        streams={
            "e1": [["Hel", "lo", RetryableLLMError("connection reset")]],
            "e2": [["Hello", " world", resp("Hello world")]],
        }
    )
    events = [ev async for ev in make_gateway(a).stream(req())]
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["TextDelta", "TextDelta", "StreamRestarted", "TextDelta", "TextDelta", "StreamDone"]
    assert isinstance(events[2], StreamRestarted) and events[2].endpoint == "e1"
    done = events[-1]
    assert isinstance(done, StreamDone) and done.response.endpoint == "e2" and done.response.failover


async def test_stream_retries_same_endpoint_before_first_token(sleeps):
    a = FakeAdapter(streams={"e1": [[RetryableLLMError("503")], ["ok", resp("ok")]]})
    events = [ev async for ev in make_gateway(a, sleeps=sleeps).stream(req())]
    assert isinstance(events[0], TextDelta) and isinstance(events[-1], StreamDone)
    assert events[-1].response.endpoint == "e1" and len(sleeps) == 1


async def test_stream_skips_endpoints_without_streaming():
    cfg = make_config("e1", "e2", e1={"features": {"streaming": False}})
    a = FakeAdapter(streams={"e2": [["x", resp("x")]]})
    events = [ev async for ev in make_gateway(a, cfg).stream(req())]
    assert events[-1].response.endpoint == "e2"
