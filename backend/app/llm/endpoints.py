"""Loading ``config/llm_endpoints.yaml``.

Values may reference environment variables as ``${VAR}`` or ``${VAR:-default}``
so one file serves all deployment profiles. ``TODO`` placeholders (corp gateway
facts not known yet) load as "unknown": an endpoint with a TODO model name is
listed but disabled, and TODO features count as unsupported.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from app.llm.types import DATA_LEVEL_RANK, DataLevel, Tier

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def _todo_to_none(v: Any) -> Any:
    return None if isinstance(v, str) and v.strip().upper() == "TODO" else v


class Features(BaseModel):
    tool_calling: bool | None = None
    json_schema: bool | None = None
    streaming: bool | None = None
    prompt_caching: bool = False
    effort: bool = False

    @field_validator("*", mode="before")
    @classmethod
    def _todo(cls, v: Any) -> Any:
        return _todo_to_none(v)

    def supports(self, name: str) -> bool:
        return bool(getattr(self, name, False))


class EndpointConfig(BaseModel):
    name: str
    adapter: Literal["anthropic", "openai_compat"] = "anthropic"
    base_url: str | None = None
    # "env" (default): honour HTTP(S)_PROXY from the environment;
    # "none": strictly direct, environment proxies ignored; anything else: proxy URL.
    proxy: str = "env"
    api_key_env: str = "ANTHROPIC_API_KEY"
    # anthropic SDK >= 2 sends keys as `Authorization: Bearer`; some gateways want `X-Api-Key`.
    auth_header: Literal["bearer", "x-api-key"] = "bearer"
    models: dict[Tier, str | None] = Field(default_factory=dict)
    max_data_level: DataLevel = "L3"
    max_context: int | None = None
    features: Features = Field(default_factory=Features)
    timeout_s: float = 120.0
    # Lower tool-loop cap when this endpoint serves the turn (backup endpoints).
    max_tool_turns: int | None = None
    # Prompt file variant to prefer, e.g. "corp" -> prompt.zh.corp.md
    prompt_variant: str | None = None
    # Anthropic server-side refusal fallback ("default" routing) for the listed models.
    server_fallback_models: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("base_url", mode="before")
    @classmethod
    def _none_str(cls, v: Any) -> Any:
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "none", "null")):
            return None
        return v

    @field_validator("proxy", mode="before")
    @classmethod
    def _proxy(cls, v: Any) -> Any:
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "env")):
            return "env"
        if isinstance(v, str) and v.strip().lower() in ("none", "direct", "null"):
            return "none"
        return v

    @field_validator("max_context", mode="before")
    @classmethod
    def _todo_int(cls, v: Any) -> Any:
        return _todo_to_none(v)

    @field_validator("models", mode="before")
    @classmethod
    def _todo_models(cls, v: Any) -> Any:
        return {k: _todo_to_none(m) for k, m in (v or {}).items()}

    def model_for(self, tier: Tier) -> str | None:
        return self.models.get(tier)

    def allows_level(self, level: DataLevel) -> bool:
        return DATA_LEVEL_RANK[level] <= DATA_LEVEL_RANK[self.max_data_level]

    def api_key(self) -> str | None:
        """``$<api_key_env>``, or the contents of the file named by ``$<api_key_env>_FILE`` (Docker secrets)."""
        if key := os.environ.get(self.api_key_env):
            return key
        if path := os.environ.get(f"{self.api_key_env}_FILE"):
            try:
                return Path(path).read_text(encoding="utf-8").strip() or None
            except OSError:
                return None
        return None

    def unavailable_reason(self) -> str | None:
        """Static reasons this endpoint cannot be used at all (config, not runtime health)."""
        if not self.enabled:
            return "disabled in config"
        if not any(self.models.values()):
            return "no model names configured (TODO)"
        if not self.api_key():
            return f"env var {self.api_key_env} is not set"
        return None


class CircuitBreakerConfig(BaseModel):
    failures: int = 3
    cooldown_s: float = 60.0


class FailoverConfig(BaseModel):
    order: list[str] = Field(default_factory=list)
    retry: int = 2
    backoff_base_s: float = 0.5
    backoff_max_s: float = 8.0
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)


class Price(BaseModel):
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None

    def cost(self, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float:
        cr = self.cache_read if self.cache_read is not None else self.input * 0.1
        cw = self.cache_write if self.cache_write is not None else self.input * 1.25
        return (
            input_tokens * self.input + output_tokens * self.output + cache_read * cr + cache_write * cw
        ) / 1e6


class LLMConfig(BaseModel):
    endpoints: list[EndpointConfig]
    failover: FailoverConfig = Field(default_factory=FailoverConfig)
    pricing: dict[str, Price] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> LLMConfig:
        names = [e.name for e in self.endpoints]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate endpoint names: {sorted(dupes)}")
        if not self.failover.order:
            self.failover.order = names
        unknown = [n for n in self.failover.order if n not in names]
        if unknown:
            raise ValueError(f"failover.order references unknown endpoints: {unknown}")
        return self

    def endpoint(self, name: str) -> EndpointConfig:
        for e in self.endpoints:
            if e.name == name:
                return e
        raise KeyError(name)

    def ordered(self) -> list[EndpointConfig]:
        return [self.endpoint(n) for n in self.failover.order]


def load_llm_config(path: Path) -> LLMConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return LLMConfig.model_validate(expand_env(raw))
