"""LLM gateway: the only place that decides *where* a request goes.

Per request it walks ``failover.order`` and, for each endpoint:

* skips it (recorded as a skipped attempt) when it is unconfigured, lacks the
  model tier, sits below the task's data level, misses a feature the request
  needs, cannot fit the context, or its circuit breaker is open;
* sends the request, retrying transient errors (connection, timeout, 408/409/
  429/5xx) with jittered exponential backoff;
* moves on to the next endpoint when retries are exhausted or the endpoint is
  unusable (401/403/404);
* raises immediately on request errors (other 4xx) - another endpoint would
  reject the same request.

Circuit breakers open after N consecutive failures; after the cooldown one trial
request is let through (half-open) and success closes the breaker, so traffic
returns to the primary endpoint automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from app.llm.adapters.anthropic import AnthropicAdapter
from app.llm.adapters.base import AdapterDone, LLMAdapter
from app.llm.adapters.openai_compat import OpenAICompatAdapter
from app.llm.endpoints import CircuitBreakerConfig, EndpointConfig, LLMConfig
from app.llm.types import (
    Attempt,
    EndpointLLMError,
    FatalLLMError,
    LLMRequest,
    LLMResponse,
    NoEndpointAvailable,
    RefusalError,
    RetryableLLMError,
    StreamDone,
    StreamEvent,
    StreamRestarted,
    TextDelta,
)

log = logging.getLogger("dayline.llm")


class CircuitBreaker:
    def __init__(self, cfg: CircuitBreakerConfig, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.clock = clock
        self.failures = 0
        self.open_until = 0.0
        self._trial_in_flight = False

    @property
    def state(self) -> str:
        if self.failures < self.cfg.failures:
            return "closed"
        return "open" if self.clock() < self.open_until else "half_open"

    def allow(self) -> bool:
        st = self.state
        if st == "closed":
            return True
        if st == "half_open" and not self._trial_in_flight:
            self._trial_in_flight = True
            return True
        return False

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    def success(self) -> None:
        self.failures = 0
        self.open_until = 0.0
        self._trial_in_flight = False

    def failure(self) -> None:
        self.failures += 1
        self._trial_in_flight = False
        if self.failures >= self.cfg.failures:
            self.open_until = self.clock() + self.cfg.cooldown_s

    def trip(self) -> None:
        self.failures = max(self.failures, self.cfg.failures - 1)
        self.failure()


def estimate_tokens(req: LLMRequest) -> int:
    """Cheap upper-ish estimate: CJK ~1 token/char, other text ~1 token/4 chars."""
    chunks = [p.text for p in req.system]
    chunks += [json.dumps(m.model_dump(exclude={"native"}), ensure_ascii=False) for m in req.messages]
    chunks += [json.dumps(t.model_dump(), ensure_ascii=False) for t in req.tools]
    if req.output_schema:
        chunks.append(json.dumps(req.output_schema, ensure_ascii=False))
    text = "".join(chunks)
    cjk = sum(1 for ch in text if ord(ch) >= 0x2E80)
    return cjk + (len(text) - cjk) // 4 + req.max_tokens


class LLMGateway:
    def __init__(
        self,
        config: LLMConfig,
        adapters: dict[str, LLMAdapter] | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.adapters: dict[str, LLMAdapter] = (
            adapters
            if adapters is not None
            else {"anthropic": AnthropicAdapter(), "openai_compat": OpenAICompatAdapter()}
        )
        self.breakers = {
            ep.name: CircuitBreaker(config.failover.circuit_breaker, clock) for ep in config.endpoints
        }
        self._sleep = sleep

    async def aclose(self) -> None:
        for a in self.adapters.values():
            await a.aclose()

    @property
    def primary(self) -> EndpointConfig:
        return self.config.ordered()[0]

    def endpoint(self, name: str) -> EndpointConfig:
        return self.config.endpoint(name)

    # --- selection ------------------------------------------------------------

    def _order(self, req: LLMRequest) -> list[EndpointConfig]:
        order = self.config.ordered()
        return order[:1] if req.fallback == "deny" else order

    def skip_reason(self, ep: EndpointConfig, req: LLMRequest, *, streaming: bool = False) -> str | None:
        if reason := ep.unavailable_reason():
            return reason
        if ep.adapter not in self.adapters:
            return f"no adapter registered for '{ep.adapter}'"
        if not ep.model_for(req.tier):
            return f"no model configured for tier '{req.tier}'"
        if not ep.allows_level(req.data_level):
            return f"data level {req.data_level} exceeds endpoint max {ep.max_data_level}"
        if req.tools and not ep.features.tool_calling:
            return "tool calling not supported/unknown (run `dl llm probe`)"
        if streaming and not ep.features.streaming:
            return "streaming not supported/unknown (run `dl llm probe`)"
        if ep.max_context and (est := estimate_tokens(req)) > ep.max_context:
            return f"request (~{est} tokens) exceeds max_context {ep.max_context}"
        return None

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        fo = self.config.failover
        if retry_after is not None:
            return min(retry_after, fo.backoff_max_s)
        return min(fo.backoff_max_s, fo.backoff_base_s * (2**attempt)) * random.uniform(0.5, 1.0)

    def _finish(
        self, resp: LLMResponse, ep: EndpointConfig, req: LLMRequest, attempts: list[Attempt]
    ) -> LLMResponse:
        primary = self.primary
        resp.endpoint = ep.name
        resp.failover = ep.name != primary.name
        resp.degraded = ep.model_for(req.tier) != primary.model_for(req.tier)
        price = self.config.pricing.get(resp.model) or self.config.pricing.get(ep.model_for(req.tier) or "")
        if price:
            u = resp.usage
            resp.cost_usd = round(
                price.cost(u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens), 6
            )
        resp.attempts = attempts
        log.info(
            "llm call ok",
            extra={
                "endpoint": ep.name,
                "model": resp.model,
                "purpose": req.purpose,
                "latency_ms": resp.latency_ms,
                "tokens_in": resp.usage.input_tokens,
                "tokens_out": resp.usage.output_tokens,
                "failover": resp.failover,
                "stop_reason": resp.stop_reason,
            },
        )
        if resp.stop_reason == "refusal":
            raise RefusalError(resp.refusal_category, resp)
        return resp

    def _fail(self, attempts: list[Attempt]) -> NoEndpointAvailable:
        # If the only thing stopping us is the data level, the caller should queue and retry later.
        queued = bool(attempts) and all(a.skipped and "data level" in (a.error or "") for a in attempts)
        return NoEndpointAvailable(attempts, queued=queued)

    # --- non-streaming ----------------------------------------------------------

    async def complete(self, req: LLMRequest) -> LLMResponse:
        attempts: list[Attempt] = []
        retries = self.config.failover.retry
        for ep in self._order(req):
            if reason := self.skip_reason(ep, req):
                attempts.append(Attempt(endpoint=ep.name, ok=False, skipped=True, error=reason))
                continue
            breaker = self.breakers[ep.name]
            if not breaker.allow():
                attempts.append(Attempt(endpoint=ep.name, ok=False, skipped=True, error="circuit open"))
                continue
            adapter = self.adapters[ep.adapter]
            model = ep.model_for(req.tier)
            assert model
            for i in range(retries + 1):
                t0 = time.monotonic()
                try:
                    resp = await adapter.complete(req, ep, model)
                except RetryableLLMError as e:
                    breaker.failure()
                    attempts.append(Attempt(endpoint=ep.name, ok=False, error=str(e), latency_ms=_ms(t0)))
                    log.warning("llm call failed (retryable)", extra={"endpoint": ep.name, "error": str(e)})
                    if i == retries or breaker.is_open:
                        break
                    await self._sleep(self._backoff(i, e.retry_after))
                except EndpointLLMError as e:
                    breaker.trip()
                    attempts.append(Attempt(endpoint=ep.name, ok=False, error=str(e), latency_ms=_ms(t0)))
                    log.warning("llm endpoint unusable", extra={"endpoint": ep.name, "error": str(e)})
                    break
                except FatalLLMError:
                    breaker.success()  # the endpoint answered; the request is at fault
                    raise
                else:
                    breaker.success()
                    attempts.append(Attempt(endpoint=ep.name, ok=True, latency_ms=resp.latency_ms))
                    return self._finish(resp, ep, req, attempts)
        raise self._fail(attempts)

    # --- streaming --------------------------------------------------------------

    async def stream(self, req: LLMRequest) -> AsyncIterator[StreamEvent]:
        """Yield text deltas, then a final ``StreamDone``.

        If a stream breaks after output has started, a ``StreamRestarted`` event
        tells the consumer to discard what it has, and the whole request is
        re-issued on the next endpoint.
        """
        attempts: list[Attempt] = []
        retries = self.config.failover.retry
        for ep in self._order(req):
            if reason := self.skip_reason(ep, req, streaming=True):
                attempts.append(Attempt(endpoint=ep.name, ok=False, skipped=True, error=reason))
                continue
            breaker = self.breakers[ep.name]
            if not breaker.allow():
                attempts.append(Attempt(endpoint=ep.name, ok=False, skipped=True, error="circuit open"))
                continue
            adapter = self.adapters[ep.adapter]
            model = ep.model_for(req.tier)
            assert model
            for i in range(retries + 1):
                t0 = time.monotonic()
                emitted = False
                try:
                    async for ev in adapter.stream(req, ep, model):
                        if isinstance(ev, AdapterDone):
                            breaker.success()
                            attempts.append(
                                Attempt(endpoint=ep.name, ok=True, latency_ms=ev.response.latency_ms)
                            )
                            yield StreamDone(response=self._finish(ev.response, ep, req, attempts))
                            return
                        assert isinstance(ev, TextDelta)
                        emitted = True
                        yield ev
                except (RetryableLLMError, EndpointLLMError) as e:
                    if isinstance(e, EndpointLLMError):
                        breaker.trip()
                    else:
                        breaker.failure()
                    attempts.append(Attempt(endpoint=ep.name, ok=False, error=str(e), latency_ms=_ms(t0)))
                    log.warning(
                        "llm stream failed", extra={"endpoint": ep.name, "error": str(e), "emitted": emitted}
                    )
                    if emitted:
                        yield StreamRestarted(endpoint=ep.name, reason=str(e))
                        break
                    if isinstance(e, EndpointLLMError) or i == retries or breaker.is_open:
                        break
                    await self._sleep(self._backoff(i, e.retry_after))
                except FatalLLMError:
                    breaker.success()
                    raise
        raise self._fail(attempts)


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
