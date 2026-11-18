"""`dl llm probe`: test each endpoint's connectivity and features.

Each check sends a tiny real request straight to one endpoint (no failover), with
the feature under test forced on, so the report shows what the endpoint really
accepts. Run it before go-live and after the corp gateway is upgraded, then copy
the suggested ``features`` block into ``llm_endpoints.yaml``.
"""

from __future__ import annotations

import json
import time
from typing import Literal

from pydantic import BaseModel, Field

from app.llm.adapters.base import AdapterDone, LLMAdapter
from app.llm.endpoints import EndpointConfig, Features
from app.llm.types import LLMError, LLMRequest, Message, TextDelta, Tier, ToolSpec

Status = Literal["ok", "fail", "skipped"]


class CheckResult(BaseModel):
    name: str
    status: Status
    detail: str = ""
    latency_ms: int = 0


class EndpointReport(BaseModel):
    endpoint: str
    tier: Tier
    model: str | None
    checks: list[CheckResult] = Field(default_factory=list)

    def ok(self, name: str) -> bool | None:
        for c in self.checks:
            if c.name == name:
                return None if c.status == "skipped" else c.status == "ok"
        return None


_TOOL = ToolSpec(
    name="record_number",
    description="Record a number. Call this whenever the user asks you to record a number.",
    input_schema={
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    },
)
_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "population_millions": {"type": "number"}},
    "required": ["city", "population_millions"],
    "additionalProperties": False,
}


def _forced(ep: EndpointConfig, **features: bool) -> EndpointConfig:
    f = Features(**{**ep.features.model_dump(), **features})
    return ep.model_copy(update={"features": f, "server_fallback_models": []})


async def _timed(coro) -> tuple[object | None, str | None, int]:
    t0 = time.monotonic()
    try:
        return await coro, None, int((time.monotonic() - t0) * 1000)
    except LLMError as e:
        return None, str(e), int((time.monotonic() - t0) * 1000)


async def probe_endpoint(adapter: LLMAdapter, ep: EndpointConfig, tier: Tier) -> EndpointReport:
    model = ep.model_for(tier)
    rep = EndpointReport(endpoint=ep.name, tier=tier, model=model)
    reason = ep.unavailable_reason() or (None if model else f"no model for tier {tier}")
    if reason:
        rep.checks.append(CheckResult(name="connectivity", status="skipped", detail=reason))
        return rep
    assert model

    # 1. connectivity
    req = LLMRequest(tier=tier, messages=[Message.user("Reply with the single word: pong")], max_tokens=1024)
    resp, err, ms = await _timed(adapter.complete(req, _forced(ep), model))
    if err:
        rep.checks.append(CheckResult(name="connectivity", status="fail", detail=err, latency_ms=ms))
        return rep
    rep.checks.append(
        CheckResult(name="connectivity", status="ok", detail=f"served by {resp.model}", latency_ms=ms)
    )

    # 2. tool calling
    req = LLMRequest(
        tier=tier,
        messages=[Message.user("Please record the number 42 using the record_number tool.")],
        tools=[_TOOL],
        max_tokens=1024,
    )
    resp, err, ms = await _timed(adapter.complete(req, _forced(ep, tool_calling=True), model))
    if err:
        rep.checks.append(CheckResult(name="tool_calling", status="fail", detail=err, latency_ms=ms))
    else:
        calls = resp.tool_calls
        good = any(c.name == "record_number" and c.input.get("value") == 42 for c in calls)
        rep.checks.append(
            CheckResult(
                name="tool_calling",
                status="ok" if good else "fail",
                detail="" if good else f"no valid tool call (stop_reason={resp.stop_reason})",
                latency_ms=ms,
            )
        )

    # 3. JSON schema (constrained output)
    req = LLMRequest(
        tier=tier,
        messages=[Message.user("Give the largest city of Japan and its population in millions.")],
        output_schema=_SCHEMA,
        max_tokens=1024,
    )
    resp, err, ms = await _timed(adapter.complete(req, _forced(ep, json_schema=True), model))
    if err:
        rep.checks.append(CheckResult(name="json_schema", status="fail", detail=err, latency_ms=ms))
    else:
        try:
            data = json.loads(resp.text)
            good = isinstance(data, dict) and {"city", "population_millions"} <= set(data)
            detail = "" if good else f"unexpected JSON: {resp.text[:120]}"
        except ValueError:
            good, detail = False, f"not JSON: {resp.text[:120]}"
        rep.checks.append(
            CheckResult(name="json_schema", status="ok" if good else "fail", detail=detail, latency_ms=ms)
        )

    # 4. effort parameter
    req = LLMRequest(tier=tier, messages=[Message.user("Reply with: ok")], effort="low", max_tokens=512)
    resp, err, ms = await _timed(adapter.complete(req, _forced(ep, effort=True), model))
    rep.checks.append(
        CheckResult(name="effort", status="fail" if err else "ok", detail=err or "", latency_ms=ms)
    )

    # 5. streaming
    req = LLMRequest(
        tier=tier, messages=[Message.user("Count from 1 to 5, separated by spaces.")], max_tokens=1024
    )
    t0 = time.monotonic()
    deltas, done, err = 0, False, None
    try:
        async for ev in adapter.stream(req, _forced(ep, streaming=True), model):
            if isinstance(ev, TextDelta):
                deltas += 1
            elif isinstance(ev, AdapterDone):
                done = True
    except LLMError as e:
        err = str(e)
    ms = int((time.monotonic() - t0) * 1000)
    good = done and deltas > 0 and not err
    rep.checks.append(
        CheckResult(
            name="streaming",
            status="ok" if good else "fail",
            detail=err or f"{deltas} text deltas",
            latency_ms=ms,
        )
    )
    rep.checks.append(
        CheckResult(
            name="prompt_caching",
            status="skipped",
            detail="not probed; check cache_read tokens in skill_runs",
        )
    )
    return rep


def suggested_features(reports: list[EndpointReport]) -> dict[str, bool]:
    """Features every probed tier of one endpoint passed."""
    out: dict[str, bool] = {}
    for feat in ("tool_calling", "json_schema", "streaming", "effort"):
        vals = [r.ok(feat) for r in reports]
        known = [v for v in vals if v is not None]
        if known:
            out[feat] = all(known)
    return out
