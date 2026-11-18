"""Manager flows end to end: Relay + MongoDB + the real runner with a scripted model."""

from __future__ import annotations

import json

import pytest

from app.relay.service import RelayForbidden
from app.reports.service import ReportError, ReportForbidden, ReportService
from app.rules.engine import RuleEngine
from app.skills.registry import SkillRegistry
from app.skills.runner import SkillForbidden, SkillInputError, SkillRunner
from app.tools.registry import ToolRegistry
from tests.conftest import SKILLS, FakeAdapter, item, make_gateway, resp, submit

DAY = "2026-11-17"


def service(org, adapter: FakeAdapter) -> ReportService:
    tools, rules = ToolRegistry.with_local_tools(), RuleEngine()
    reg = SkillRegistry(SKILLS, known_tools=tools.permissions(), known_rules=rules.names(), hot_reload=False)
    runner = SkillRunner(
        registry=reg, gateway=make_gateway(adapter), tools=tools, rules=rules, relay=org.relay
    )
    return ReportService(runner=runner, relay=org.relay, reports=org.reports, reviews=org.reviews)


def review_out(**kw) -> str:
    out = {
        "rating": "attention",
        "summary": "完成了重试机制上线。",
        "issues": [],
        "trend_flags": [],
        "questions": ["导出什么时候能开始?"],
        "feedback_draft": "辛苦了!",
        "escalation_candidates": [],
    }
    return json.dumps({**out, **kw}, ensure_ascii=False)


ROLLUP = {
    "summary": "支付组今天上线了回调重试,对账报表进行中。",
    "highlights": [{"text": "回调失败率降到 0.3%", "members": ["zhangsan"]}],
    "risks": [{"text": "导出被风控文档阻塞", "members": ["zhangsan"], "severity": "high"}],
    "escalation_candidates": [
        {"text": "风控接口文档迟迟未给", "members": ["zhangsan"], "reason": "跨团队", "need_from": "风控组"},
        {"text": "测试环境不稳定", "members": [], "reason": "影响多人"},
    ],
}


async def seed_team(org):
    await submit(org.reports, "zhangsan", "2026-11-16", [item("plan", "完成对账报表导出")])
    await submit(
        org.reports,
        "zhangsan",
        DAY,
        [
            item("done", "上线支付回调重试,失败率降到 0.3%"),
            item("blocker", "导出被风控接口文档阻塞", severity="high"),
        ],
    )
    await submit(org.reports, "wangwu", DAY, [item("done", "修复登录页样式")])
    await submit(org.reports, "lisi", DAY, [item("done", "评审支付方案")])  # the manager's own items


async def test_review_manager_uses_relay_data_and_stores_review(org):
    await seed_team(org)
    flag = {"type": "delayed_plan", "text": "完成对账报表导出", "comment": "建议了解导出的排期"}
    a = FakeAdapter(
        {
            "e1": [
                resp(
                    review_out(
                        trend_flags=[flag],
                        issues=[
                            {
                                "type": "vague",
                                "quote": "上线支付回调重试",
                                "explanation": "x",
                                "suggestion": "y",
                            }
                        ],
                    )
                )
            ]
        }
    )
    result, review_id = await service(org, a).review_member("lisi", "zhangsan", DAY, tone="direct")
    assert result.status == "ok" and review_id
    prompt = a.calls[0][2].messages[0].text
    assert '<untrusted kind="context" name="member_report">' in prompt
    assert "完成对账报表导出" in prompt and '"delayed_plan"' in prompt  # signals computed by code
    assert "## 进展评级: ⚠️ 需要关注" in result.markdown and "**计划延期**" in result.markdown
    saved = await org.reviews.for_subject("zhangsan", reviewer_id="lisi")
    assert saved[0]["rating"] == "attention" and saved[0]["report_version"] == 1


async def test_review_manager_rejects_invented_trends_then_accepts_fix(org):
    await seed_team(org)
    bad = {"type": "stalled", "text": "某个不存在的事项", "comment": "x"}
    a = FakeAdapter({"e1": [resp(review_out(trend_flags=[bad])), resp(review_out())]})
    result, _ = await service(org, a).review_member("lisi", "zhangsan", DAY)
    assert result.status == "ok" and result.validation_attempts == 2
    assert "signals-grounded" in a.calls[1][2].messages[-1].text


async def test_only_direct_managers_can_review(org):
    await seed_team(org)
    svc = service(org, FakeAdapter())
    with pytest.raises(SkillForbidden):
        await svc.review_member("zhangsan", "wangwu", DAY)  # not a manager
    with pytest.raises(SkillForbidden):
        await svc.review_member("qianqi", "zhangsan", DAY)  # manager, but not theirs
    with pytest.raises(SkillInputError):
        await svc.review_member("lisi", "zhaoliu", DAY)  # no report that day


async def test_rollup_draft_overlays_code_owned_fields_and_confirm_picks_escalations(org):
    await seed_team(org)
    # the model tries to write stats itself; code-owned values must win
    a = FakeAdapter({"e1": [resp(json.dumps({**ROLLUP, "stats": {"members": 99}}, ensure_ascii=False))]})
    svc = service(org, a)
    result, draft, warnings = await svc.draft_rollup("lisi", DAY)
    assert result.status == "ok" and warnings == []
    ts = draft["team_summary"]
    assert ts["stats"] == {"members": 3, "submitted": 2, "on_track": 1}  # zhangsan has a high blocker
    assert ts["missing"] == [{"id": "zhaoliu", "name": "赵六"}]
    assert {s["user_id"] for s in ts["source_reports"]} == {"zhangsan", "wangwu"}
    assert draft["kind"] == "manager" and draft["status"] == "draft"
    assert draft["own_items"][0]["text"] == "评审支付方案"
    assert "建议上报(待你确认)" in result.markdown and "张三" in result.markdown

    # the manager's manager cannot see the draft yet
    boss = await org.relay.viewer("boss")
    before = await org.relay.get_report(boss, "lisi", DAY)
    assert before.team_summary is None

    confirmed, stale = await svc.confirm_rollup("lisi", draft["id"], escalate=[1])
    final = confirmed["team_summary"]
    assert [e["text"] for e in final["escalations"]] == ["风控接口文档迟迟未给"]
    assert "escalation_candidates" not in final and stale == []
    assert confirmed["ai_draft"]["team_summary"]["escalation_candidates"]  # draft kept for diffing
    seen = await org.relay.get_report(boss, "lisi", DAY)
    assert seen.team_summary["stats"]["submitted"] == 2 and "需上级介入" in seen.rendered_md
    with pytest.raises(RelayForbidden):
        await org.relay.get_report(boss, "zhangsan", DAY)  # still no skip-level access


async def test_rollup_must_not_write_up_missing_members(org):
    await seed_team(org)
    bad = {**ROLLUP, "highlights": [{"text": "赵六完成了很多工作", "members": ["zhaoliu"]}]}
    a = FakeAdapter(
        {"e1": [resp(json.dumps(bad, ensure_ascii=False)), resp(json.dumps(ROLLUP, ensure_ascii=False))]}
    )
    result, _, _ = await service(org, a).draft_rollup("lisi", DAY)
    assert result.status == "ok" and result.validation_attempts == 2
    assert (
        "members-known" in a.calls[1][2].messages[-1].text
        and "没有提交日报" in a.calls[1][2].messages[-1].text
    )
    # the model sees submitted members' items; missing members appear by name only
    assert (
        "修复登录页样式" in a.calls[0][2].messages[0].text and '"zhaoliu"' in a.calls[0][2].messages[0].text
    )


async def test_rollup_confirm_guards(org):
    await seed_team(org)
    svc = service(org, FakeAdapter({"e1": [resp(json.dumps(ROLLUP, ensure_ascii=False))]}))
    _, draft, _ = await svc.draft_rollup("lisi", DAY)
    with pytest.raises(ReportForbidden):
        await svc.confirm_rollup("boss", draft["id"])
    with pytest.raises(ReportError, match="#5"):
        await svc.confirm_rollup("lisi", draft["id"], escalate=[5])
    # a member resubmits after the draft: confirming warns about the stale source
    await submit(org.reports, "wangwu", DAY, [item("done", "修复登录页样式和按钮")])
    _, stale = await svc.confirm_rollup("lisi", draft["id"])
    assert stale == ["wangwu"]


async def test_manager_rewrites_own_items_after_rollup_keeps_team_summary(org):
    await seed_team(org)
    svc = service(org, FakeAdapter({"e1": [resp(json.dumps(ROLLUP, ensure_ascii=False))]}))
    await svc.draft_rollup("lisi", DAY)
    newer = await submit(org.reports, "lisi", DAY, [item("done", "评审支付方案并定稿")], confirm=False)
    assert newer["kind"] == "manager" and newer["team_summary"]["summary"] == ROLLUP["summary"]


async def test_generate_daily_tool_reads_own_history_through_relay(org):
    await submit(org.reports, "zhangsan", "2026-11-16", [item("plan", "完成对账报表导出")])
    out = {
        "kind": "individual",
        "date": DAY,
        "lang": "zh",
        "own_items": [item("done", "x")],
        "plan_check": [],
        "questions": [],
    }
    a = FakeAdapter(
        {"e1": [resp(calls=[("get_recent_reports", {"days": 1})]), resp(json.dumps(out, ensure_ascii=False))]}
    )
    svc = service(org, a)
    result = await svc.runner.run("generate-daily", {"narrative": "做了 x", "date": DAY}, user_id="zhangsan")
    assert result.status == "ok"
    tool_result = a.calls[1][2].messages[-1].content[0].content
    assert "完成对账报表导出" in tool_result and "2026-11-16" in tool_result
