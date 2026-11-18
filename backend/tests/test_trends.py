from __future__ import annotations

from app.rules.trends import compute_trends, on_track, similarity
from tests.conftest import item


def rep(date: str, *items: dict) -> dict:
    return {"date": date, "own_items": list(items)}


def types(signals) -> dict[str, list]:
    out: dict[str, list] = {}
    for s in signals:
        out.setdefault(s.type, []).append(s)
    return out


def test_similarity_handles_chinese_and_wording_changes():
    assert similarity("完成对账报表导出", "对账报表导出功能完成") > 0.5
    assert similarity("完成对账报表导出", "修复登录页样式") < 0.2
    assert similarity("", "x") == 0.0


def test_plan_carried_over_several_reports_is_flagged_with_streak():
    history = [
        rep("2026-11-16", item("plan", "完成对账报表导出")),
        rep("2026-11-13", item("plan", "完成对账报表导出")),
        rep("2026-11-12", item("plan", "别的事")),
    ]
    today = rep("2026-11-17", item("progress", "对账报表导出,完成一半"), item("done", "修复登录样式"))
    s = types(compute_trends(today, history, today="2026-11-17"))
    (d,) = s["delayed_plan"]
    assert d.reports == 2 and d.dates == ["2026-11-16", "2026-11-13"] and d.detail["in_progress_today"]


def test_plan_done_today_is_not_flagged():
    history = [rep("2026-11-16", item("plan", "完成对账报表导出"))]
    today = rep("2026-11-17", item("done", "对账报表导出完成并提测"))
    assert "delayed_plan" not in types(compute_trends(today, history, today="2026-11-17"))


def test_stalled_overdue_repeated_and_lingering():
    history = [
        rep(
            "2026-11-16",
            item("progress", "风控规则引擎接入", progress="40%"),
            item("done", "上线支付回调重试"),
            item("blocker", "等风控接口文档", severity="high"),
        ),
        rep("2026-11-13", item("progress", "风控规则引擎接入", progress="40%")),
    ]
    today = rep(
        "2026-11-17",
        item("progress", "风控规则引擎接入", progress="40%", eta="2026-11-15"),
        item("done", "上线支付回调重试"),
        item("blocker", "还在等风控接口文档", severity="high"),
    )
    s = types(compute_trends(today, history, today="2026-11-17"))
    assert s["stalled"][0].reports == 3
    assert s["overdue"][0].detail["eta"] == "2026-11-15"
    assert s["repeated_done"][0].dates == ["2026-11-17", "2026-11-16"]
    assert s["lingering_blocker"][0].reports == 2
    assert not on_track(today, compute_trends(today, history, today="2026-11-17"))


def test_progress_change_breaks_stalled_streak_and_clean_day_is_on_track():
    history = [
        rep("2026-11-16", item("progress", "规则引擎", progress="40%")),
        rep("2026-11-13", item("progress", "规则引擎", progress="20%")),
    ]
    today = rep("2026-11-17", item("progress", "规则引擎", progress="60%"))
    signals = compute_trends(today, history, today="2026-11-17")
    assert signals == []
    assert on_track(today, signals) and not on_track(None, [])
