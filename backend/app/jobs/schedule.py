"""Deadline orchestration (plan §4.5): per org unit, staggered by level.

A manager's team has two daily times:

  deadline  members' reports are due; the worker reminds those who have not submitted
  rollup    the worker drafts the manager's team roll-up (the manager confirms it)

Base times come from ``config/schedules.yaml`` (per ``org_unit`` of the manager, else the
default). Managers higher up run later: each level above the first adds
``level_gap_minutes``, so a middle manager's roll-up exists before their own manager's
deadline. "Height" = 1 for a manager of individual contributors, 2 for a manager of such
managers, and so on.

    default: {deadline: "18:00", rollup: "18:30"}
    level_gap_minutes: 60
    org_units:
      支付组: {deadline: "18:30", rollup: "19:00"}
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from app.db.models import UserDoc

_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class UnitSchedule(BaseModel):
    deadline: str = Field(default="18:00", pattern=_HHMM)
    rollup: str = Field(default="18:30", pattern=_HHMM)

    @field_validator("rollup")
    @classmethod
    def _after_deadline(cls, v: str, info) -> str:
        d = info.data.get("deadline")
        if d and v < d:
            raise ValueError("rollup must not be earlier than the deadline")
        return v


class ScheduleConfig(BaseModel):
    default: UnitSchedule = Field(default_factory=UnitSchedule)
    level_gap_minutes: int = Field(default=60, ge=0, le=240)
    org_units: dict[str, UnitSchedule] = Field(default_factory=dict)


def load_schedule(path: Path, *, deadline: str = "18:00", rollup: str = "18:30") -> ScheduleConfig:
    if path.is_file():
        return ScheduleConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return ScheduleConfig(default=UnitSchedule(deadline=deadline, rollup=rollup))


def heights(users: list[UserDoc]) -> dict[str, int]:
    """Management height of every manager in the active org."""
    children: dict[str, list[str]] = {}
    for u in users:
        if u.manager_id:
            children.setdefault(u.manager_id, []).append(u.id)
    memo: dict[str, int] = {}

    def h(uid: str, seen: frozenset[str] = frozenset()) -> int:
        if uid in memo:
            return memo[uid]
        kids = [k for k in children.get(uid, []) if k not in seen]
        memo[uid] = 0 if not kids else 1 + max(h(k, seen | {uid}) for k in kids)
        return memo[uid]

    return {m: h(m) for m in children}


def _shift(hhmm: str, minutes: int) -> str:
    total = min(int(hhmm[:2]) * 60 + int(hhmm[3:]) + minutes, 23 * 60 + 59)
    return f"{total // 60:02d}:{total % 60:02d}"


def team_times(cfg: ScheduleConfig, manager: UserDoc, height: int) -> tuple[str, str]:
    base = cfg.org_units.get(manager.org_unit or "", cfg.default)
    offset = cfg.level_gap_minutes * max(height - 1, 0)
    return _shift(base.deadline, offset), _shift(base.rollup, offset)
