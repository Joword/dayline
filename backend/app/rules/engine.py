"""RuleEngine: deterministic checks that run before (``pre``) and after (``post``) the LLM.

Rules are plain functions registered with ``@rule`` and referenced by name from
``skill.yaml``. A finding with level ``error`` fails validation (the runner then
retries once with the findings as feedback); ``warning`` findings are attached
to the result for the user to see.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

Phase = Literal["pre", "post"]


@dataclass
class RuleFinding:
    rule: str
    level: Literal["error", "warning"]
    message: str
    path: str | None = None

    def __str__(self) -> str:
        loc = f" ({self.path})" if self.path else ""
        return f"[{self.rule}]{loc} {self.message}"


@dataclass
class RuleContext:
    skill: str
    lang: str
    inputs: dict[str, Any]
    extras: dict[str, Any] = field(default_factory=dict)


RuleFn = Callable[[Any, RuleContext], list[RuleFinding]]


@dataclass
class Rule:
    name: str
    phase: Phase
    fn: RuleFn
    description: str


_RULES: dict[str, Rule] = {}


def rule(name: str, *, phase: Phase = "post", description: str = ""):
    def deco(fn: RuleFn) -> RuleFn:
        if name in _RULES:
            raise ValueError(f"duplicate rule: {name}")
        _RULES[name] = Rule(name, phase, fn, description or (fn.__doc__ or "").strip())
        return fn

    return deco


class RuleEngine:
    def __init__(self) -> None:
        import app.rules as pkg

        for mod in pkgutil.iter_modules(pkg.__path__):
            if mod.name != "engine":
                importlib.import_module(f"{pkg.__name__}.{mod.name}")
        self.rules = dict(_RULES)

    def names(self) -> set[str]:
        return set(self.rules)

    def run(self, names: list[str], phase: Phase, data: Any, ctx: RuleContext) -> list[RuleFinding]:
        out: list[RuleFinding] = []
        for n in names:
            r = self.rules.get(n)
            if r is None or r.phase != phase:
                continue
            out.extend(r.fn(data, ctx))
        return out
