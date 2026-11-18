"""Built-in rules for the M1 skills (generate-daily, review-member)."""

from __future__ import annotations

import re
from typing import Any

from app.rules.engine import RuleContext, RuleFinding, rule

MAX_INPUT_CHARS = 20_000

# Inputs that carry machine-collected evidence, per item source (M3 fills these in).
SOURCE_INPUTS = {"git": ("git_activity",), "calendar": ("calendar_events",)}


def _msg(ctx: RuleContext, zh: str, en: str) -> str:
    return zh if ctx.lang == "zh" else en


@rule("input-size", phase="pre", description="Reject oversized text inputs before calling the model")
def input_size(inputs: dict[str, Any], ctx: RuleContext) -> list[RuleFinding]:
    out = []
    for k, v in inputs.items():
        if isinstance(v, str) and len(v) > MAX_INPUT_CHARS:
            out.append(
                RuleFinding(
                    "input-size",
                    "error",
                    _msg(
                        ctx,
                        f"输入过长({len(v)} 字),上限 {MAX_INPUT_CHARS}",
                        f"input too long ({len(v)} chars), limit {MAX_INPUT_CHARS}",
                    ),
                    k,
                )
            )
    return out


@rule("max-questions", description="At most 3 follow-up questions")
def max_questions(out: dict[str, Any], ctx: RuleContext) -> list[RuleFinding]:
    qs = out.get("questions") or []
    if len(qs) > 3:
        return [
            RuleFinding(
                "max-questions",
                "error",
                _msg(ctx, f"追问最多 3 个,当前 {len(qs)} 个", f"at most 3 questions, got {len(qs)}"),
                "questions",
            )
        ]
    return []


@rule("sources-grounded", description="Item sources must match the data actually provided")
def sources_grounded(out: dict[str, Any], ctx: RuleContext) -> list[RuleFinding]:
    findings = []
    meta = ctx.extras.get("context_meta") or {}
    ctx_sources = {s for m in meta.values() for s in (m.get("sources") or [])}
    ctx_urls = {u for m in meta.values() for u in (m.get("urls") or [])}
    for i, item in enumerate(out.get("own_items") or []):
        src = item.get("source")
        needed = SOURCE_INPUTS.get(src or "")
        if needed and src not in ctx_sources and not any(ctx.inputs.get(k) for k in needed):
            findings.append(
                RuleFinding(
                    "sources-grounded",
                    "error",
                    _msg(
                        ctx,
                        f"条目来源标为 {src},但本次没有提供 {src} 数据;来自用户自述的条目应标为 manual",
                        f"item source is '{src}' but no {src} data was provided; items from the user's own text are 'manual'",
                    ),
                    f"own_items[{i}].source",
                )
            )
        if (
            item.get("ref_url")
            and item["ref_url"] not in ctx_urls
            and not _url_in_inputs(item["ref_url"], ctx.inputs)
        ):
            findings.append(
                RuleFinding(
                    "sources-grounded",
                    "error",
                    _msg(
                        ctx,
                        "ref_url 不在输入中,不要编造链接",
                        "ref_url does not appear in the input; do not invent links",
                    ),
                    f"own_items[{i}].ref_url",
                )
            )
    return findings


@rule("done-has-items", description="Warn when a report has no completed or in-progress work")
def done_has_items(out: dict[str, Any], ctx: RuleContext) -> list[RuleFinding]:
    kinds = {i.get("kind") for i in out.get("own_items") or []}
    if not kinds & {"done", "progress"} and not out.get("questions"):
        return [
            RuleFinding(
                "done-has-items",
                "warning",
                _msg(ctx, "日报里没有已完成或进行中的事项", "the report has no done or in-progress items"),
            )
        ]
    return []


_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub("", s).lower()


@rule("quotes-grounded", description="Quoted report text must exist in the reviewed report")
def quotes_grounded(out: dict[str, Any], ctx: RuleContext) -> list[RuleFinding]:
    source = _norm(str(ctx.inputs.get("report") or ctx.extras.get("quote_source") or ""))
    findings = []
    for i, issue in enumerate(out.get("issues") or []):
        q = issue.get("quote")
        if q and _norm(q) not in source:
            findings.append(
                RuleFinding(
                    "quotes-grounded",
                    "error",
                    _msg(
                        ctx,
                        f"引用“{q[:30]}”在原日报中找不到,请逐字引用原文",
                        f"quote '{q[:30]}' is not in the report; quote verbatim",
                    ),
                    f"issues[{i}].quote",
                )
            )
    return findings


@rule("members-known", description="A roll-up may only name team members who submitted a report")
def members_known(out: dict[str, Any], ctx: RuleContext) -> list[RuleFinding]:
    meta = (ctx.extras.get("context_meta") or {}).get("team_snapshot") or {}
    submitted, missing = set(meta.get("member_ids") or []), set(meta.get("missing_ids") or [])
    findings = []
    for section in ("highlights", "risks", "escalation_candidates"):
        for i, entry in enumerate(out.get(section) or []):
            for m in entry.get("members") or []:
                if m in missing:
                    msg = _msg(
                        ctx,
                        f"{m} 今天没有提交日报,不要替他总结内容(未提交的人只列在 missing 里)",
                        f"{m} did not submit a report; do not write up their work (missing members are listed only)",
                    )
                elif m not in submitted:
                    msg = _msg(ctx, f"{m} 不是本团队成员", f"{m} is not a member of this team")
                else:
                    continue
                findings.append(RuleFinding("members-known", "error", msg, f"{section}[{i}].members"))
    return findings


@rule("signals-grounded", description="Trend comments must refer to signals computed from the history")
def signals_grounded(out: dict[str, Any], ctx: RuleContext) -> list[RuleFinding]:
    from app.rules.trends import similarity

    meta = (ctx.extras.get("context_meta") or {}).get("member_trends") or {}
    known = meta.get("signal_texts") or []
    findings = []
    for i, flag in enumerate(out.get("trend_flags") or []):
        text = flag.get("text", "")
        if not any(similarity(text, k) >= 0.5 for k in known):
            findings.append(
                RuleFinding(
                    "signals-grounded",
                    "error",
                    _msg(
                        ctx,
                        f"趋势“{text[:30]}”不在提供的跨日信号里;只评论已给出的信号",
                        f"trend '{text[:30]}' is not among the provided signals; comment only on given signals",
                    ),
                    f"trend_flags[{i}].text",
                )
            )
    return findings


_ACCUSATORY = re.compile(
    r"撒谎|说谎|谎报|虚报|造假|作假|欺骗|隐瞒|偷懒|摸鱼|不诚实|夸大|糊弄"
    r"|\b(lie[sd]?|lying|liar|fake[sd]?|fabricat\w*|dishonest\w*|cheat\w*|lazy|slacking|hiding|exaggerat\w*)\b",
    re.IGNORECASE,
)


@rule("neutral-wording", description="Cross-checks and feedback describe facts, never accuse")
def neutral_wording(out: dict[str, Any], ctx: RuleContext) -> list[RuleFinding]:
    texts: list[tuple[str, str]] = [("feedback_draft", out.get("feedback_draft") or "")]
    for key, fields in (("consistency_notes", ("text", "suggestion")), ("trend_flags", ("comment",))):
        for i, entry in enumerate(out.get(key) or []):
            texts += [(f"{key}[{i}].{f}", entry.get(f) or "") for f in fields]
    findings = []
    for path, text in texts:
        if m := _ACCUSATORY.search(text):
            findings.append(
                RuleFinding(
                    "neutral-wording",
                    "error",
                    _msg(
                        ctx,
                        f"“{m.group(0)}”带有结论或指责意味;只陈述不一致之处,写“信息不一致,建议询问”",
                        f"'{m.group(0)}' draws a conclusion or accuses; state the mismatch only (\"worth asking about\")",
                    ),
                    path,
                )
            )
    return findings


@rule("mapping-valid", description="Template mapping fields must exist in the DingTalk template")
def mapping_valid(out: dict[str, Any], ctx: RuleContext) -> list[RuleFinding]:
    from app.dingtalk.templates import template_field_names

    given = [f.strip() for f in str(ctx.inputs.get("fields") or "").replace(",", ",").split(",") if f.strip()]
    known = given or template_field_names(str(ctx.inputs.get("template") or ""))
    findings = []
    seen: set[str] = set()
    for i, f in enumerate(out.get("fields") or []):
        name = f.get("dingtalk_field", "")
        if known and name not in known:
            findings.append(
                RuleFinding(
                    "mapping-valid",
                    "error",
                    _msg(
                        ctx,
                        f"模板中没有字段“{name}”;可用字段:{'、'.join(known)}",
                        f"the template has no field '{name}'; fields: {', '.join(known)}",
                    ),
                    f"fields[{i}].dingtalk_field",
                )
            )
        if name in seen:
            findings.append(
                RuleFinding(
                    "mapping-valid",
                    "error",
                    _msg(ctx, f"字段“{name}”重复", f"field '{name}' appears twice"),
                    f"fields[{i}]",
                )
            )
        seen.add(name)
    return findings


def _url_in_inputs(url: str, inputs: dict[str, Any]) -> bool:
    return any(isinstance(v, str) and url in v for v in inputs.values())
