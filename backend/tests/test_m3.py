"""M3: intent routing, API endpoints, collector, manager cross-check, neutral wording."""

from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.collector import CollectorConfig, collect_and_push
from app.rules.engine import RuleContext, RuleEngine
from app.skills.registry import SkillRegistry
from app.skills.router import SkillRouter, primary_text_input
from app.tools.registry import ToolRegistry
from tests.conftest import SKILLS, FakeAdapter, item, make_gateway, resp, submit
from tests.test_activity import activity, connect, github_handler  # noqa: F401  (fixture re-export)
from tests.test_api_org import make_client, token_for

DAY = "2026-11-17"


def router() -> SkillRouter:
    tools, rules = ToolRegistry.with_local_tools(), RuleEngine()
    return SkillRouter(
        SkillRegistry(SKILLS, known_tools=tools.permissions(), known_rules=rules.names(), hot_reload=False)
    )


async def test_intent_routing_uses_small_model_and_respects_roles():
    a = FakeAdapter({"e1": [resp(json.dumps({"skill": "review-member", "confidence": 0.9}))]})
    r = await router().resolve("帮我看看小李这份日报写得怎么样:今天持续推进重构", make_gateway(a))
    assert r and r.skill.name == "review-member" and r.via == "intent"
    req = a.calls[0][2]
    assert req.tier == "small" and req.purpose == "router"
    enum = req.output_schema["properties"]["skill"]["enum"]
    assert (
        "team-rollup" not in enum and "review-manager" not in enum
    )  # members are not offered manager skills
    assert '<untrusted kind="input" name="message">' in req.messages[0].text
    assert "prompt" not in req.messages[0].text.lower()  # only names / descriptions / intents


async def test_intent_routing_low_confidence_or_none_routes_nowhere():
    for out in ({"skill": "generate-daily", "confidence": 0.3}, {"skill": "none", "confidence": 0.95}):
        a = FakeAdapter({"e1": [resp(json.dumps(out))]})
        assert await router().resolve("今天天气怎么样", make_gateway(a), is_manager=True) is None


async def test_commands_win_without_a_model_call():
    a = FakeAdapter()
    r = await router().resolve("/review 今天写了代码", make_gateway(a))
    assert r and r.via == "command" and a.calls == []
    assert primary_text_input(r.skill) == "report"


def test_neutral_wording_rule():
    engine = RuleEngine()
    ctx = RuleContext(skill="review-manager", lang="zh", inputs={})
    bad = {
        "feedback_draft": "辛苦了",
        "consistency_notes": [{"text": "他在日报里虚报了工作", "suggestion": "问清楚"}],
    }
    good = {
        "feedback_draft": "辛苦了",
        "consistency_notes": [{"text": "日报与 git 信息不一致", "suggestion": "建议询问"}],
    }
    assert [f.rule for f in engine.run(["neutral-wording"], "post", bad, ctx)] == ["neutral-wording"]
    assert engine.run(["neutral-wording"], "post", good, ctx) == []
    en = {"feedback_draft": "Looks like you were slacking"}
    assert engine.run(["neutral-wording"], "post", en, RuleContext(skill="x", lang="en", inputs={}))


async def test_review_manager_cross_checks_only_with_consent(activity):  # noqa: F811
    from tests.test_manager_flows import service

    await connect(activity)
    await submit(activity.reports, "zhangsan", DAY, [item("done", "完成退款接口联调")])
    note = {
        "text": "日报写完成退款接口联调,git 中当天的提交在支付服务仓库",
        "evidence": "x",
        "suggestion": "建议询问联调在哪里做的",
    }
    review = {
        "rating": "on_track",
        "summary": "s",
        "issues": [],
        "trend_flags": [],
        "questions": [],
        "feedback_draft": "辛苦了",
        "escalation_candidates": [],
        "consistency_notes": [note],
    }
    a = FakeAdapter(
        {"e1": [resp(json.dumps(review, ensure_ascii=False)), resp(json.dumps(review, ensure_ascii=False))]}
    )
    svc = service(activity, a)
    await svc.review_member("lisi", "zhangsan", DAY)
    no_consent = a.calls[0][2].messages[0].text
    assert '"git": null' in no_consent and "has not consented" in no_consent and "pull/482" not in no_consent

    await activity.relay.grant_consent(await activity.relay.viewer("zhangsan"), "git")
    result, _ = await svc.review_member("lisi", "zhangsan", DAY)
    with_consent = a.calls[1][2].messages[0].text
    assert "pull/482" in with_consent and '"calendar": null' in with_consent  # calendar still not shared
    assert result.output["consistency_notes"][0]["suggestion"] == "建议询问联调在哪里做的"


async def test_m3_api_endpoints(activity):  # noqa: F811

    z = await token_for(activity, "zhangsan")
    lisi = await token_for(activity, "lisi")
    async with make_client(activity, FakeAdapter(), api_token="svc", env="prod") as c:
        body = {"config": {"type": "git", "provider": "github", "username": "zs", "token": "ghp_secret"}}
        added = (await c.post("/me/integrations", json=body, headers=z)).json()
        assert added["label"] == "github:github.com" and "ghp_secret" not in json.dumps(added)
        listed = (await c.get("/me/integrations", headers=z)).json()
        assert len(listed) == 1 and "token" not in json.dumps(listed)
        mine = (await c.get(f"/me/activity?date={DAY}", headers=z)).json()
        assert mine["git"]["totals"]["commits"] == 1
        theirs = (await c.get(f"/activity/zhangsan/{DAY}", headers=lisi)).json()
        assert theirs["git"] is None and "git" in theirs["unavailable"]
        bad = {"config": {"type": "calendar", "provider": "coremail", "mode": "caldav", "caldav_url": "x"}}
        assert (await c.post("/me/integrations", json=bad, headers=z)).status_code == 422
        # collector ingest: service token + collector client only
        payload = {
            "user_id": "wangwu",
            "date": DAY,
            "source": "calendar",
            "payload": {"events": [], "count": 0, "total_minutes": 0},
        }
        assert (await c.post("/internal/activity", json=payload, headers=z)).status_code == 403
        svc = {"authorization": "Bearer svc"}
        assert (await c.post("/internal/activity", json=payload, headers=svc)).status_code == 403
        ok = await c.post(
            "/internal/activity", json=payload, headers={**svc, "x-dayline-client": "collector"}
        )
        assert ok.status_code == 200


async def test_collector_pushes_summaries_only():
    calls = []
    source = httpx.MockTransport(github_handler(calls))
    pushed = []

    def push(request: httpx.Request):
        pushed.append(
            (
                request.headers.get("x-dayline-client"),
                request.headers.get("authorization"),
                json.loads(request.content),
            )
        )
        return httpx.Response(200, json={"ok": True})

    cfg = CollectorConfig.model_validate(
        {
            "target": "https://dayline.test",
            "token": "svc",
            "users": [
                {
                    "user_id": "zhangsan",
                    "git": [{"type": "git", "provider": "github", "username": "zs", "token": "ghp_secret"}],
                }
            ],
        }
    )
    report = await collect_and_push(
        cfg, DAY, transport_factory=lambda route: source, push_transport=httpx.MockTransport(push)
    )
    assert report == [{"user_id": "zhangsan", "source": "git", "pushed": True, "errors": []}]
    client_id, auth, body = pushed[0]
    assert client_id == "collector" and auth == "Bearer svc"
    assert body["payload"]["repos"][0]["prs"][0]["number"] == 482 and "ghp_secret" not in json.dumps(body)


def test_collector_config_expands_env(monkeypatch, tmp_path):
    from app.integrations.collector import load_collector_config

    monkeypatch.setenv("COLLECTOR_TOKEN", "svc-from-env")
    monkeypatch.setenv("GITLAB_PAT", "glpat-env")
    f = tmp_path / "collector.yaml"
    f.write_text(
        "target: https://dayline.test\ntoken: ${COLLECTOR_TOKEN}\nusers:\n"
        "  - user_id: zhangsan\n    git:\n      - {type: git, provider: gitlab, base_url: https://gitlab.corp, token: '${GITLAB_PAT}'}\n",
        encoding="utf-8",
    )
    cfg = load_collector_config(f)
    assert cfg.token == "svc-from-env" and cfg.users[0].git[0].token.get_secret_value() == "glpat-env"


@pytest.mark.parametrize("skill", ["generate-daily", "review-manager"])
def test_activity_context_is_optional(skill):
    tools, rules = ToolRegistry.with_local_tools(), RuleEngine()
    reg = SkillRegistry(SKILLS, known_tools=tools.permissions(), known_rules=rules.names(), hot_reload=False)
    specs = {c.name: c for c in reg.get(skill).manifest.context}
    name = "own_activity" if skill == "generate-daily" else "member_activity"
    assert specs[name].optional
