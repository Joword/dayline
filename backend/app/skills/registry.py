"""SkillRegistry: scans ``skills/{builtin,team,user}``, validates, hot-reloads.

Hot reload polls file mtimes (at most every ``poll_interval_s``) instead of using
inotify, because edits made on the Windows side of ``/mnt/f`` raise no Linux
file events.

Name clashes are rejected rather than shadowed: a team or user skill cannot take
the name of a built-in one (or a user skill that of a team one).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from app.skills.manifest import Layer, ManifestError, Skill, load_skill
from app.tools.contract import is_mcp_ref

log = logging.getLogger("dayline.skills")

MCP_REF_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}__[A-Za-z0-9_-]{1,62}$")

LAYERS: tuple[Layer, ...] = ("builtin", "team", "user")


class SkillNotFound(KeyError):
    pass


@dataclass
class SkillProblem:
    path: str
    layer: Layer
    error: str


class SkillRegistry:
    def __init__(
        self,
        root: Path,
        *,
        known_tools: set[str] | dict[str, str | None],
        known_rules: set[str],
        known_context: dict[str, str] | None = None,
        hot_reload: bool = True,
        poll_interval_s: float = 2.0,
    ):
        """``known_tools`` / ``known_context`` map names to the permission they need."""
        self.root = root
        self.known_tools: dict[str, str | None] = (
            dict(known_tools) if isinstance(known_tools, dict) else dict.fromkeys(known_tools)
        )
        self.known_rules = known_rules
        if known_context is None:
            from app.skills.context import providers

            known_context = {n: p.permission for n, p in providers().items()}
        self.known_context = known_context
        self.hot_reload = hot_reload
        self.poll_interval_s = poll_interval_s
        self._skills: dict[str, Skill] = {}
        self.problems: list[SkillProblem] = []
        self._fingerprint: tuple = ()
        self._last_poll = 0.0
        self.load()

    # --- scanning -------------------------------------------------------------

    def _skill_dirs(self) -> list[tuple[Layer, Path]]:
        out = []
        for layer in LAYERS:
            base = self.root / layer
            if base.is_dir():
                out += [(layer, d) for d in sorted(base.iterdir()) if (d / "skill.yaml").is_file()]
        return out

    def _compute_fingerprint(self) -> tuple:
        items = []
        for layer in LAYERS:
            base = self.root / layer
            if not base.is_dir():
                continue
            for f in base.rglob("*"):
                if f.is_file() and "__pycache__" not in f.parts:
                    st = f.stat()
                    items.append((str(f), st.st_mtime_ns, st.st_size))
        return tuple(sorted(items))

    def load(self) -> None:
        skills: dict[str, Skill] = {}
        problems: list[SkillProblem] = []
        for layer, d in self._skill_dirs():
            try:
                s = load_skill(d, layer)
                self._check_refs(s)
            except (ManifestError, OSError, ValueError) as e:
                problems.append(SkillProblem(str(d), layer, str(e)))
                continue
            if s.name in skills:
                problems.append(
                    SkillProblem(
                        str(d), layer, f"name '{s.name}' already used by the {skills[s.name].layer} skill"
                    )
                )
                continue
            skills[s.name] = s
        self._skills = skills
        self.problems = problems
        self._fingerprint = self._compute_fingerprint()
        self._last_poll = time.monotonic()
        for p in problems:
            log.warning("skill rejected", extra={"path": p.path, "error": p.error})
        log.info("skills loaded", extra={"count": len(skills), "problems": len(problems)})

    def _check_refs(self, s: Skill) -> None:
        m = s.manifest
        # "<package>__<tool>" names a member MCP tool: enabled and disabled at run time, so its
        # presence and permissions are checked per run (ToolRegistry.for_run), not here
        if unknown := sorted(t for t in set(m.tools) - set(self.known_tools) if not is_mcp_ref(t)):
            raise ManifestError(f"unknown tools {unknown}")
        if bad := sorted(t for t in m.tools if is_mcp_ref(t) and not MCP_REF_RE.fullmatch(t)):
            raise ManifestError(f"invalid member tool names {bad}; expected '<package>__<tool>'")
        if unknown := sorted(set(m.rules) - self.known_rules):
            raise ManifestError(f"unknown rules {unknown}")
        if unknown := sorted({c.name for c in m.context} - set(self.known_context)):
            raise ManifestError(f"unknown context providers {unknown}")
        needed = {self.known_context[c.name]: f"context '{c.name}'" for c in m.context}
        needed |= {p: f"tool '{t}'" for t in m.tools if (p := self.known_tools.get(t))}
        for perm, what in needed.items():
            if perm not in m.permissions:
                raise ManifestError(f"{what} needs permission '{perm}'; add it to permissions")

    def maybe_reload(self) -> bool:
        if not self.hot_reload or time.monotonic() - self._last_poll < self.poll_interval_s:
            return False
        self._last_poll = time.monotonic()
        fp = self._compute_fingerprint()
        if fp == self._fingerprint:
            return False
        log.info("skill files changed; reloading")
        self.load()
        return True

    # --- access -----------------------------------------------------------------

    def get(self, name: str) -> Skill:
        self.maybe_reload()
        try:
            return self._skills[name]
        except KeyError:
            raise SkillNotFound(name) from None

    def list(self) -> list[Skill]:
        self.maybe_reload()
        return sorted(self._skills.values(), key=lambda s: (LAYERS.index(s.layer), s.name))
