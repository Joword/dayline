"""Adapter interface: translate the neutral format to one provider's API and back.

Adapters do no retrying or failover of their own (the gateway owns that) and
must raise only the error classes from ``app.llm.types``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.llm.endpoints import EndpointConfig
from app.llm.types import LLMRequest, LLMResponse, TextDelta


class AdapterDone:
    def __init__(self, response: LLMResponse):
        self.response = response


class LLMAdapter(Protocol):
    name: str

    async def complete(self, req: LLMRequest, ep: EndpointConfig, model: str) -> LLMResponse: ...

    def stream(
        self, req: LLMRequest, ep: EndpointConfig, model: str
    ) -> AsyncIterator[TextDelta | AdapterDone]: ...

    async def aclose(self) -> None: ...


JSON_INSTRUCTION = (
    "Reply with a single JSON object only - no other text and no code fences. "
    "Write string values in the language the instructions ask for. The object must match this JSON Schema:\n"
)
