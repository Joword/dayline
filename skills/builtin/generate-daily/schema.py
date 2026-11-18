"""Output schema of generate-daily (plan §4.1)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    commits: int | None = Field(default=None, description="相关提交数(仅 git 来源)")
    meetings: int | None = Field(default=None, description="相关会议数(仅 calendar 来源)")
    hours: float | None = Field(default=None, description="会议时长(小时,仅 calendar 来源)")


class ReportItem(BaseModel):
    kind: Literal["done", "progress", "blocker", "plan"]
    text: str = Field(description="一句话,突出结果;保留原文中的具体数字和对象")
    source: Literal["manual", "git", "calendar"] = Field(description="信息来源;来自用户自述的条目为 manual")
    ref_url: str | None = Field(default=None, description="只在输入中明确给出链接时填写")
    evidence: Evidence | None = None
    progress: str | None = Field(default=None, description="进行中事项的进度,如 60%;看不出就不填")
    eta: str | None = Field(default=None, description="预计完成日期 YYYY-MM-DD;没有就不填")
    need_from: str | None = Field(default=None, description="阻塞项需要谁配合")
    severity: Literal["low", "medium", "high"] | None = Field(default=None, description="阻塞项严重度")
    priority: Literal["P0", "P1", "P2", "P3"] | None = Field(default=None, description="计划项优先级")
    pending_confirmation: bool = Field(default=False, description="内容含推测、需要用户确认时为 true")
    cross_team: bool = Field(default=False, description="依赖其他团队或影响其他团队时为 true(同级管理者可见)")


class PlanCheck(BaseModel):
    plan_text: str = Field(description="上一份日报中的计划条目原文")
    status: Literal["done", "in_progress", "not_started", "dropped", "unknown"]
    note: str | None = Field(default=None, description="简短说明,例如延期原因")


class DailyReport(BaseModel):
    kind: Literal["individual"] = "individual"
    date: str = Field(description="报告日期 YYYY-MM-DD")
    lang: Literal["zh", "en"]
    own_items: list[ReportItem]
    plan_check: list[PlanCheck] = Field(default_factory=list, description="与上一份日报计划的对照;没有历史日报时为空")
    questions: list[str] = Field(default_factory=list, max_length=3, description="缺口追问,最多 3 个;信息足够时为空")
