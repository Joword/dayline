"""Output schema of review-member (plan §4.2)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Issue(BaseModel):
    type: Literal["vague", "unverifiable", "blocker_unclear", "missing_plan", "inconsistent", "other"]
    quote: str = Field(description="逐字引用日报原文中的相关片段")
    explanation: str = Field(description="为什么这是问题")
    suggestion: str = Field(description="怎样改写更好")


class ReviewResult(BaseModel):
    summary: str = Field(description="一句话说明对方实际产出了什么")
    issues: list[Issue] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list, max_length=5, description="可以向作者追问的问题,最多 5 个")
    feedback_draft: str = Field(description="可以直接发给作者的反馈,语气友好、具体")
