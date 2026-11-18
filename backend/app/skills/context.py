"""Context providers: data a skill needs, fetched by code through Relay.

A skill lists providers in ``skill.yaml`` (``context: [team_snapshot]``). Before
the model runs, the runner calls each provider as the requesting user, so Relay's
visibility rules apply, and puts the result into the prompt as untrusted data.
This is how "code fetches, the model judges" works for skills: the model never
chooses whose data it sees.

A provider can also return:
* ``overlay``      - code-owned output fields (counts, missing members, source report
                     versions) merged over the model's output after validation, so the
                     model cannot get them wrong;
* ``quote_source`` - text that quotes in the output must come from;
* ``meta``         - values for the caller (e.g. the reviewed report's id).

Each provider declares the permission it needs; a skill must list that permission
in its manifest, which the registry checks at load time.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.relay.policy import Viewer


@dataclass
class ContextRequest:
    relay: Any  # app.relay.service.Relay
    viewer: Viewer
    inputs: dict[str, Any]
    params: dict[str, Any]
    run_date: str
    lang: str


@dataclass
class ContextResult:
    data: Any
    overlay: dict[str, Any] | None = None
    quote_source: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


ProviderFn = Callable[[ContextRequest], Awaitable[ContextResult]]


ReplayFn = Callable[[Any], ContextResult]


@dataclass
class ContextProvider:
    name: str
    fn: ProviderFn
    permission: str
    description: str
    replay: ReplayFn  # data -> ContextResult, no I/O (examples, evals, tests)


_PROVIDERS: dict[str, ContextProvider] = {}


def context_provider(name: str, *, permission: str, replay: ReplayFn, description: str = ""):
    def deco(fn: ProviderFn) -> ProviderFn:
        if name in _PROVIDERS:
            raise ValueError(f"duplicate context provider: {name}")
        _PROVIDERS[name] = ContextProvider(
            name, fn, permission, description or (fn.__doc__ or "").strip(), replay
        )
        return fn

    return deco


def providers() -> dict[str, ContextProvider]:
    importlib.import_module("app.skills.providers")
    return dict(_PROVIDERS)


def replay(name: str, data: Any) -> ContextResult:
    """Rebuild a provider's result from recorded ``data`` (no database)."""
    return providers()[name].replay(data)
