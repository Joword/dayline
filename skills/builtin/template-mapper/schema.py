"""Output of template-mapper: a draft of config/templates/dingtalk_mapping.yaml."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.dingtalk.templates import SOURCES

SourceName = Literal[tuple(SOURCES)]  # type: ignore[valid-type]


class FieldDraft(BaseModel):
    dingtalk_field: str = Field(description="钉钉模板中的字段名,必须与模板一字不差")
    source: list[SourceName] = Field(min_length=1, description="填入这个字段的 Dayline 日报内容")  # type: ignore[valid-type]
    format: Literal["numbered_list", "bullet_list", "text"] = "numbered_list"


class TemplateMappingDraft(BaseModel):
    fields: list[FieldDraft] = Field(min_length=1)
    unmapped_policy: Literal["append_to_last", "drop"] = Field(
        default="append_to_last", description="没有被任何字段覆盖的内容如何处理"
    )
    notes: list[str] = Field(default_factory=list, description="需要人确认的地方,例如含义不明的字段")
