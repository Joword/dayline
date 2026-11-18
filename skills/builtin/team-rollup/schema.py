"""What the model writes for team-rollup (plan §4.3).

Counts, missing members and the source report versions are not here: the
context provider computes them and the runner merges them over this output.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Highlight(BaseModel):
    text: str = Field(description="团队层面的一条亮点,突出结果")
    members: list[str] = Field(default_factory=list, description="相关成员的 id")


class Risk(BaseModel):
    text: str
    members: list[str] = Field(default_factory=list)
    severity: Literal["low", "medium", "high"]


class EscalationCandidate(BaseModel):
    text: str = Field(description="可能需要上级介入的阻塞或风险")
    members: list[str] = Field(default_factory=list)
    reason: str
    need_from: str | None = Field(default=None, description="需要谁/哪个团队配合")


class TeamRollupDraft(BaseModel):
    summary: str = Field(description="两三句话的整体情况")
    highlights: list[Highlight] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    escalation_candidates: list[EscalationCandidate] = Field(default_factory=list)
