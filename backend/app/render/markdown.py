"""Deterministic Markdown rendering of skill outputs (no LLM involved)."""

from __future__ import annotations

from typing import Any

T = {
    "zh": {
        "report": "日报",
        "paren": "({})",
        "done": "✅ 已完成",
        "progress": "🔄 进行中",
        "blocker": "⛔ 阻塞",
        "plan": "📅 明日计划",
        "plan_check": "🔁 与上次计划对照",
        "questions": "❓ 待补充",
        "need_from": "需要",
        "severity": "严重度",
        "eta": "预计",
        "pending": "待确认",
        "summary": "摘要",
        "issues": "问题",
        "suggestion": "建议",
        "followups": "追问清单",
        "feedback": "反馈草稿",
        "status": {
            "done": "已完成",
            "in_progress": "进行中",
            "not_started": "未开始",
            "dropped": "已取消",
            "unknown": "未提及",
        },
        "sev": {"low": "低", "medium": "中", "high": "高"},
        "issue_type": {
            "vague": "表述空泛",
            "unverifiable": "难以验证",
            "blocker_unclear": "阻塞不清楚",
            "missing_plan": "缺少计划",
            "inconsistent": "前后不一致",
            "other": "其他",
        },
    },
    "en": {
        "report": "Daily report",
        "paren": " ({})",
        "done": "✅ Done",
        "progress": "🔄 In progress",
        "blocker": "⛔ Blockers",
        "plan": "📅 Plan for tomorrow",
        "plan_check": "🔁 Against the last plan",
        "questions": "❓ To fill in",
        "need_from": "needs",
        "severity": "severity",
        "eta": "ETA",
        "pending": "to be confirmed",
        "summary": "Summary",
        "issues": "Issues",
        "suggestion": "Suggestion",
        "followups": "Follow-up questions",
        "feedback": "Feedback draft",
        "status": {
            "done": "done",
            "in_progress": "in progress",
            "not_started": "not started",
            "dropped": "dropped",
            "unknown": "not mentioned",
        },
        "sev": {"low": "low", "medium": "medium", "high": "high"},
        "issue_type": {
            "vague": "vague",
            "unverifiable": "hard to verify",
            "blocker_unclear": "unclear blocker",
            "missing_plan": "missing plan",
            "inconsistent": "inconsistent",
            "other": "other",
        },
    },
}


def _t(lang: str) -> dict[str, Any]:
    return T.get(lang, T["en"])


def _item_line(item: dict[str, Any], t: dict[str, Any]) -> str:
    text = item.get("text", "")
    meta = []
    kind = item.get("kind")
    if kind == "progress":
        if item.get("progress"):
            meta.append(str(item["progress"]))
        if item.get("eta"):
            meta.append(f"{t['eta']} {item['eta']}")
    if kind == "blocker":
        if item.get("need_from"):
            meta.append(f"{t['need_from']}: {item['need_from']}")
        if item.get("severity"):
            meta.append(f"{t['severity']}: {t['sev'].get(item['severity'], item['severity'])}")
    if kind == "plan" and item.get("priority"):
        text = f"[{item['priority']}] {text}"
    if item.get("pending_confirmation"):
        meta.append(t["pending"])
    line = text + (t["paren"].format(", ".join(meta)) if meta else "")
    if item.get("ref_url"):
        line += f" [↗]({item['ref_url']})"
    return line


def render_daily_report(out: dict[str, Any], lang: str) -> str:
    t = _t(lang)
    lines = [f"# {t['report']} {out.get('date', '')}".rstrip()]
    items = out.get("own_items") or []
    for kind in ("done", "progress", "blocker", "plan"):
        group = [i for i in items if i.get("kind") == kind]
        if not group:
            continue
        lines += ["", f"## {t[kind]}"]
        for n, i in enumerate(group, 1):
            prefix = f"{n}." if kind == "done" else "-"
            lines.append(f"{prefix} {_item_line(i, t)}")
    checks = out.get("plan_check") or []
    if checks:
        lines += ["", f"## {t['plan_check']}"]
        for c in checks:
            st = t["status"].get(c.get("status"), c.get("status"))
            note = f" — {c['note']}" if c.get("note") else ""
            lines.append(f"- {c.get('plan_text', '')}: **{st}**{note}")
    team = out.get("team_summary")
    if team:
        lines += ["", render_team_summary(team, lang).strip()]
    qs = out.get("questions") or []
    if qs:
        lines += ["", f"## {t['questions']}"] + [f"{n}. {q}" for n, q in enumerate(qs, 1)]
    return "\n".join(lines).strip() + "\n"


def render_review(out: dict[str, Any], lang: str) -> str:
    t = _t(lang)
    lines = [f"## {t['summary']}", out.get("summary", "")]
    issues = out.get("issues") or []
    if issues:
        lines += ["", f"## {t['issues']}"]
        for n, i in enumerate(issues, 1):
            typ = t["issue_type"].get(i.get("type"), i.get("type", ""))
            lines.append(f"{n}. **{typ}** — “{i.get('quote', '')}”")
            if i.get("explanation"):
                lines.append(f"   {i['explanation']}")
            if i.get("suggestion"):
                lines.append(f"   {t['suggestion']}: {i['suggestion']}")
    qs = out.get("questions") or []
    if qs:
        lines += ["", f"## {t['followups']}"] + [f"{n}. {q}" for n, q in enumerate(qs, 1)]
    if out.get("feedback_draft"):
        lines += ["", f"## {t['feedback']}", "", out["feedback_draft"]]
    return "\n".join(lines).strip() + "\n"


def render_generic(data: Any, lang: str, level: int = 1) -> str:
    """Fallback for skills without a dedicated template: headings for keys, bullets for lists."""
    if isinstance(data, dict):
        parts = []
        for k, v in data.items():
            if v in (None, "", [], {}):
                continue
            if isinstance(v, dict | list):
                parts.append(f"{'#' * min(level + 1, 6)} {k}\n\n{render_generic(v, lang, level + 1)}")
            else:
                parts.append(f"**{k}**: {v}")
        return "\n\n".join(parts)
    if isinstance(data, list):
        lines = []
        for v in data:
            if isinstance(v, dict):
                inner = "; ".join(f"{k}: {x}" for k, x in v.items() if x not in (None, "", [], {}))
                lines.append(f"- {inner}")
            else:
                lines.append(f"- {v}")
        return "\n".join(lines)
    return str(data)


M2 = {
    "zh": {
        "rating": {"on_track": "✅ 正常推进", "attention": "⚠️ 需要关注", "risk": "🔴 有风险"},
        "rating_label": "进展评级",
        "trends": "跨日趋势",
        "trend_type": {
            "delayed_plan": "计划延期",
            "stalled": "长期无进展",
            "overdue": "已过预计完成日",
            "repeated_done": "重复汇报",
            "lingering_blocker": "阻塞持续",
        },
        "escalate_maybe": "可能需要上级介入(由你决定)",
        "team": "👥 团队汇总",
        "stats": "提交 {submitted}/{members} · 正常推进 {on_track}",
        "highlights": "✨ 亮点",
        "risks": "⚠️ 风险",
        "candidates": "⬆️ 建议上报(待你确认)",
        "escalations": "⬆️ 需上级介入",
        "pending": "📥 收到的升级事项(确认时可继续上报)",
        "raised": "⬆️ 今天已上报",
        "esc_status": {
            "open": "跟踪中",
            "escalated": "已上报",
            "in_progress": "处理中",
            "resolved": "已解决",
        },
        "holder": "当前处理人",
        "missing": "📭 未提交",
        "need_from": "需要",
        "sep": "、",
    },
    "en": {
        "rating": {"on_track": "✅ On track", "attention": "⚠️ Needs attention", "risk": "🔴 At risk"},
        "rating_label": "Status",
        "trends": "Cross-day trends",
        "trend_type": {
            "delayed_plan": "plan slipped",
            "stalled": "stalled",
            "overdue": "past ETA",
            "repeated_done": "reported again",
            "lingering_blocker": "persistent blocker",
        },
        "escalate_maybe": "May need your manager (your call)",
        "team": "👥 Team summary",
        "stats": "submitted {submitted}/{members} · on track {on_track}",
        "highlights": "✨ Highlights",
        "risks": "⚠️ Risks",
        "candidates": "⬆️ Suggested escalations (to confirm)",
        "escalations": "⬆️ Escalations",
        "pending": "📥 Escalated to you (can be forwarded on confirm)",
        "raised": "⬆️ Raised today",
        "esc_status": {
            "open": "tracking",
            "escalated": "escalated",
            "in_progress": "in progress",
            "resolved": "resolved",
        },
        "holder": "holder",
        "missing": "📭 Not submitted",
        "need_from": "needs",
        "sep": ", ",
    },
}


def render_manager_review(out: dict[str, Any], lang: str) -> str:
    t, m2 = _t(lang), M2.get(lang, M2["en"])
    lines = [f"## {m2['rating_label']}: {m2['rating'].get(out.get('rating'), out.get('rating', ''))}", ""]
    lines.append(render_review({k: v for k, v in out.items() if k != "feedback_draft"}, lang).strip())
    flags = out.get("trend_flags") or []
    if flags:
        lines += ["", f"## {m2['trends']}"]
        for f in flags:
            typ = m2["trend_type"].get(f.get("type"), f.get("type", ""))
            lines.append(f"- **{typ}** — {f.get('text', '')}")
            if f.get("comment"):
                lines.append(f"  {f['comment']}")
    cands = out.get("escalation_candidates") or []
    if cands:
        lines += ["", f"## {m2['escalate_maybe']}"] + [
            f"- {c.get('text', '')} — {c.get('reason', '')}" for c in cands
        ]
    if out.get("feedback_draft"):
        lines += ["", f"## {t['feedback']}", "", out["feedback_draft"]]
    return "\n".join(lines).strip() + "\n"


def _names(ids: list[str], names: dict[str, str], sep: str) -> str:
    return sep.join(names.get(i, i) for i in ids)


def render_team_summary(ts: dict[str, Any], lang: str, *, final: bool | None = None) -> str:
    """A confirmed summary (it has `escalations`) shows the chosen escalations; a draft
    shows the candidates and the items waiting for the manager."""
    if final is None:
        final = "escalations" in ts
    m2 = M2.get(lang, M2["en"])
    sep, names = m2["sep"], ts.get("member_names") or {}
    stats = ts.get("stats") or {}
    lines = [f"## {m2['team']}"]
    if stats:
        lines.append(m2["stats"].format(**{"members": 0, "submitted": 0, "on_track": 0, **stats}))
    if ts.get("summary"):
        lines += ["", ts["summary"]]

    def who(e: dict[str, Any]) -> str:
        return f"({_names(e['members'], names, sep)})" if e.get("members") else ""

    if ts.get("highlights"):
        lines += ["", f"### {m2['highlights']}"] + [
            f"- {h.get('text', '')}{who(h)}" for h in ts["highlights"]
        ]
    if ts.get("risks"):
        lines += ["", f"### {m2['risks']}"]
        for r in ts["risks"]:
            sev = _t(lang)["sev"].get(r.get("severity"), r.get("severity", ""))
            lines.append(f"- [{sev}] {r.get('text', '')}{who(r)}")

    def esc_line(n: int, e: dict[str, Any]) -> str:
        need = f" · {m2['need_from']}: {e['need_from']}" if e.get("need_from") else ""
        reason = f" — {e['reason']}" if e.get("reason") else ""
        status = ""
        if e.get("status"):
            status = f" `{m2['esc_status'].get(e['status'], e['status'])}`"
            if e.get("holder"):
                status += f" · {m2['holder']}: {e['holder']}"
        return f"{n}. {e.get('text', '')}{who(e)}{reason}{need}{status}"

    sections = (
        [("escalations", m2["escalations"])]
        if final
        else [
            ("escalation_candidates", m2["candidates"]),
            ("pending_escalations", m2["pending"]),
            ("raised_escalations", m2["raised"]),
        ]
    )
    for key, title in sections:
        entries = ts.get(key) or []
        if entries:
            lines += ["", f"### {title}"] + [esc_line(n, e) for n, e in enumerate(entries, 1)]
    if ts.get("missing"):
        lines += ["", f"### {m2['missing']}", sep.join(x.get("name", x.get("id", "")) for x in ts["missing"])]
    return "\n".join(lines).strip() + "\n"


def render_team_rollup(out: dict[str, Any], lang: str) -> str:
    return render_team_summary(out, lang, final=False)


TEMPLATES = {
    "daily_report": render_daily_report,
    "review": render_review,
    "manager_review": render_manager_review,
    "team_rollup": render_team_rollup,
}


def render_markdown(template: str, output: Any, lang: str) -> str:
    if isinstance(output, str):
        return output
    fn = TEMPLATES.get(template)
    return fn(output, lang) if fn else render_generic(output, lang).strip() + "\n"
