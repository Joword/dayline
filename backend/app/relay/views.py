"""Relay output models. Every field carries its data level (L1 org/project,
L2 team summary, L3 personal detail) so a future external client (E1) can be cut
down to the levels it is allowed to see."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.relay.policy import Access


def L(level: str) -> dict[str, str]:
    return {"data_level": level}


class UserBrief(BaseModel):
    id: str = Field(json_schema_extra=L("L1"))
    name: str = Field(json_schema_extra=L("L1"))
    org_unit: str | None = Field(default=None, json_schema_extra=L("L1"))


class ReportView(BaseModel):
    id: str = Field(json_schema_extra=L("L1"))
    user: UserBrief
    date: str = Field(json_schema_extra=L("L1"))
    kind: str = Field(json_schema_extra=L("L1"))
    version: int = Field(json_schema_extra=L("L1"))
    status: str = Field(json_schema_extra=L("L1"))
    lang: str = Field(json_schema_extra=L("L1"))
    access: Access = Field(json_schema_extra=L("L1"))
    own_items: list[dict[str, Any]] = Field(default_factory=list, json_schema_extra=L("L3"))
    plan_check: list[dict[str, Any]] = Field(default_factory=list, json_schema_extra=L("L3"))
    team_summary: dict[str, Any] | None = Field(default=None, json_schema_extra=L("L2"))
    rendered_md: str = Field(default="", json_schema_extra=L("L3"))
    raw_input: dict[str, Any] | None = Field(default=None, json_schema_extra=L("L3"))  # owner only
    confirmed_at: datetime | None = Field(default=None, json_schema_extra=L("L1"))
    sync_status: str = Field(default="none", json_schema_extra=L("L1"))  # DingTalk log sync


class TeamMember(BaseModel):
    user: UserBrief
    report: ReportView | None = None


class TeamSnapshot(BaseModel):
    manager: UserBrief
    date: str
    members: list[TeamMember]

    @property
    def submitted(self) -> list[TeamMember]:
        return [m for m in self.members if m.report is not None]

    @property
    def missing(self) -> list[UserBrief]:
        return [m.user for m in self.members if m.report is None]
