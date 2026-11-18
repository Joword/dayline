"""Prompt assembly for a skill run.

Layout (stable first, so the prefix can be cached):

  system[0]  core rules (identity, no-fabrication, untrusted-content rule)
  system[1]  the skill's prompt + few-shot examples      <- cache breakpoint
  user       run date + every input; free text wrapped in <untrusted> tags

Per-endpoint prompt variants (``prompt.zh.corp.md``) become ``system_variants``.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.untrusted import UNTRUSTED_RULES, wrap_untrusted
from app.llm.types import LLMRequest, Message, SystemPart
from app.skills.manifest import Skill

CORE = {
    "zh": (
        "你是 Dayline,帮助团队成员撰写和审阅工作日报的助手。\n"
        "基本原则:\n"
        "1. 只依据本次提供的输入和工具返回的数据。不要编造事实、数字、链接、人名或进度;信息不足时提出追问或标注“待确认”。\n"
        "2. 只对事项做判断,不评价个人能力,不做排名或打分。commit 数量不等于工作产出。\n"
        "3. 除非另有说明,使用简体中文输出。\n"
        f"4. {UNTRUSTED_RULES['zh']}"
    ),
    "en": (
        "You are Dayline, an assistant that helps team members write and review daily work reports.\n"
        "Principles:\n"
        "1. Use only the input given in this request and data returned by tools. Never invent facts, numbers, "
        "links, names or progress; when information is missing, ask or mark the item as 'to be confirmed'.\n"
        "2. Judge work items, never people: no ability judgements, rankings or scores. Commit counts are not output.\n"
        "3. Write in English unless told otherwise.\n"
        f"4. {UNTRUSTED_RULES['en']}"
    ),
}

LABELS = {
    "zh": {
        "examples": "## 示例",
        "example": "示例",
        "input": "输入",
        "output": "输出",
        "date": "报告日期",
        "closing": "请根据以上输入完成任务。",
        "lang_note": "注意:请使用简体中文输出。",
    },
    "en": {
        "examples": "## Examples",
        "example": "Example",
        "input": "Input",
        "output": "Output",
        "date": "Report date",
        "closing": "Complete the task using the input above.",
        "lang_note": "Note: write the output in English.",
    },
}


def _render_inputs(skill: Skill, inputs: dict[str, Any], lang: str) -> str:
    lines = []
    for spec in skill.manifest.inputs:
        if spec.name not in inputs:
            continue
        v = inputs[spec.name]
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
        if spec.untrusted:
            lines.append(wrap_untrusted(text, kind="input", name=spec.name))
        else:
            lines.append(f"{spec.name}: {text}")
    return "\n\n".join(lines)


def _examples_block(skill: Skill, lang: str) -> str:
    shots = [e for e in skill.examples if e.fewshot and e.output is not None and e.lang == lang]
    if not shots:
        return ""
    L = LABELS[lang]
    parts = [L["examples"]]
    for i, e in enumerate(shots[:3], 1):
        parts.append(
            f"### {L['example']} {i}\n{L['input']}:\n<example_input>\n{_example_input(skill, e, lang)}\n</example_input>\n"
            f"{L['output']}:\n<example_output>\n{json.dumps(e.output, ensure_ascii=False, indent=2)}\n</example_output>"
        )
    return "\n\n".join(parts)


def _example_input(skill: Skill, e: Any, lang: str) -> str:
    parts = [_render_inputs(skill, e.inputs, lang)]
    if e.context:
        parts.append(_render_context(e.context))
    return "\n\n".join(p for p in parts if p)


def _skill_part(skill: Skill, lang: str, variant: str | None) -> SystemPart:
    text, prompt_lang = skill.prompt_for(lang, variant)
    blocks = [text]
    if ex := _examples_block(skill, lang):
        blocks.append(ex)
    if prompt_lang != lang:
        blocks.append(LABELS[lang]["lang_note"])
    return SystemPart(text="\n\n".join(blocks), cacheable=True)


def _render_context(context: dict[str, Any]) -> str:
    return "\n\n".join(
        wrap_untrusted(json.dumps(data, ensure_ascii=False, indent=1, default=str), kind="context", name=name)
        for name, data in context.items()
    )


def build_request(
    skill: Skill,
    inputs: dict[str, Any],
    *,
    lang: str,
    run_date: str,
    context: dict[str, Any] | None = None,
) -> LLMRequest:
    m = skill.manifest
    core = SystemPart(text=CORE.get(lang, CORE["en"]))
    variants = {
        key.split(".", 1)[1]: [core, _skill_part(skill, lang, key.split(".", 1)[1])]
        for key in skill.prompts
        if "." in key and key.startswith(lang + ".")
    }
    L = LABELS[lang]
    parts = [f"{L['date']}: {run_date}", _render_inputs(skill, inputs, lang)]
    if context:
        parts.append(_render_context(context))
    parts.append(L["closing"])
    user_text = "\n\n".join(p for p in parts if p)
    return LLMRequest(
        tier=m.model,
        system=[core, _skill_part(skill, lang, None)],
        system_variants=variants,
        messages=[Message.user(user_text)],
        output_schema=skill.output_schema,
        max_tokens=m.max_tokens,
        effort=m.effort,
        data_level=m.data_level,
        fallback=m.fallback,
        purpose=m.name,
    )
