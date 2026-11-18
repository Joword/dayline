"""Output schema of review-manager (plan §4.2, manager column)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Issue(BaseModel):
    type: Literal["vague", "unverifiable", "blocker_unclear", "missing_plan", "inconsistent", "other"]
    quote: str = Field(description="逐字引用日报原文中的相关片段")
    explanation: str
    suggestion: str


class TrendFlag(BaseModel):
    type: Literal["delayed_plan", "stalled", "overdue", "repeated_done", "lingering_blocker"]
    text: str = Field(description="对应信号中的事项原文")
    comment: str = Field(description="这个信号意味着什么、建议怎么跟进;措辞中性")


class EscalationCandidate(BaseModel):
    text: str = Field(description="日报中的阻塞事项")
    reason: str = Field(description="为什么可能需要上级介入")


class ConsistencyNote(BaseModel):
    text: str = Field(description="不一致之处,中性描述,例如“日报写完成了退款联调,git 中当天没有相关提交”")
    evidence: str = Field(description="依据:日报中的哪条,对应 git / 日历中的什么")
    suggestion: str = Field(description="建议怎么问,例如“可以问问联调是在哪个仓库或环境做的”")


class ManagerReview(BaseModel):
    rating: Literal["on_track", "attention", "risk"] = Field(description="工作进展评级:针对事项,不是对人")
    summary: str = Field(description="一句话说明下属今天实际产出了什么")
    issues: list[Issue] = Field(default_factory=list)
    trend_flags: list[TrendFlag] = Field(default_factory=list, description="只评论提供的跨日信号")
    questions: list[str] = Field(default_factory=list, max_length=5)
    feedback_draft: str
    escalation_candidates: list[EscalationCandidate] = Field(default_factory=list)
    consistency_notes: list[ConsistencyNote] = Field(
        default_factory=list, description="日报与 git / 日历的不一致之处;没有授权数据时为空"
    )
