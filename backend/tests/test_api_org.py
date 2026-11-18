"""M2 API: identity, reports and team endpoints against a real MongoDB."""

from __future__ import annotations

import json

import httpx
import pytest

from app.api.main import create_app
from app.bootstrap import Services
from app.core.config import Settings
from app.core.security import hash_token, new_token
from app.db.models import ApiToken
from app.reports.service import ReportService
from app.rules.engine import RuleEngine
from app.skills.registry import SkillRegistry
from app.skills.router import SkillRouter
from app.skills.runner import SkillRunner
from app.tools.registry import ToolRegistry
from tests.conftest import SKILLS, FakeAdapter, item, make_gateway, resp, submit

DAY = "2026-11-17"
GOOD = {
    "kind": "individual",
    "date": DAY,
    "lang": "zh",
    "own_items": [item("done", "上线重试")],
    "plan_check": [],
    "questions": [],
}
ROLLUP = {
    "summary": "s",
    "highlights": [],
    "risks": [],
    "escalation_candidates": [{"text": "e", "members": [], "reason": "r"}],
}


def make_client(org, adapter: FakeAdapter, **settings_kw) -> httpx.AsyncClient:
    """In-loop ASGI client: the async Mongo client is bound to the test's event loop."""
    tools, rules = ToolRegistry.with_local_tools(), RuleEngine()
    reg = SkillRegistry(SKILLS, known_tools=tools.permissions(), known_rules=rules.names(), hot_reload=False)
    runner = SkillRunner(
        registry=reg, gateway=make_gateway(adapter), tools=tools, rules=rules, relay=org.relay
    )
    s = Services(
        settings=Settings(**settings_kw),
        gateway=runner.gateway,
        tools=tools,
        rules=rules,
        registry=reg,
        router=SkillRouter(reg),
        runner=runner,
        reports=org.reports,
        users=org.users,
        reviews=org.reviews,
        audit=org.audit,
        relay=org.relay,
        report_service=ReportService(
            runner=runner, relay=org.relay, reports=org.reports, reviews=org.reviews
        ),
        activity=getattr(org, "activity", None),
        integrations=getattr(getattr(org, "activity", None), "integrations", None),
    )
    app = create_app(s.settings, services=s)
    app.state.services = s
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def token_for(org, user_id: str) -> dict[str, str]:
    tid, tok = new_token()
    await org.users.add_token(user_id, ApiToken(id=tid, hash=hash_token(tok)))
    return {"authorization": f"Bearer {tok}"}


async def test_personal_tokens_and_service_token(org):
    z = await token_for(org, "zhangsan")
    svc = {"authorization": "Bearer svc"}
    async with make_client(org, FakeAdapter(), api_token="svc", env="prod") as c:
        assert (await c.get("/me")).status_code == 401
        assert (await c.get("/me", headers={"authorization": "Bearer dl_nope_x"})).status_code == 401
        me = (await c.get("/me", headers=z)).json()
        assert me["user"]["id"] == "zhangsan" and me["manager"]["id"] == "lisi"
        # the service token acts for a named user; without one it cannot call user endpoints
        assert (await c.get("/skills", headers=svc)).status_code == 200
        assert (await c.get("/me", headers=svc)).status_code == 400
        as_lisi = (await c.get("/me", headers={**svc, "x-dayline-user": "lisi"})).json()
        assert as_lisi["user"]["id"] == "lisi" and len(as_lisi["direct_reports"]) == 3
        # tokens: create, use, revoke
        created = (await c.post("/me/tokens", json={"label": "laptop"}, headers=z)).json()
        new = {"authorization": f"Bearer {created['token']}"}
        assert (await c.get("/me", headers=new)).status_code == 200
        assert (await c.delete(f"/me/tokens/{created['id']}", headers=z)).json() == {"revoked": True}
        assert (await c.get("/me", headers=new)).status_code == 401
    u = await org.users.get("zhangsan")
    assert all(t.hash != created["token"] for t in u.api_tokens)  # only hashes are stored


async def test_generate_confirm_and_visibility(org):
    z = await token_for(org, "zhangsan")
    lisi = await token_for(org, "lisi")
    boss = await token_for(org, "boss")
    a = FakeAdapter({"e1": [resp(json.dumps(GOOD, ensure_ascii=False))]})
    async with make_client(org, a, api_token="svc", env="prod") as c:
        r = (
            await c.post("/reports/generate", json={"narrative": "上线了重试", "date": DAY}, headers=z)
        ).json()
        rid = r["report"]["id"]
        assert (
            r["result"]["status"] == "ok"
            and r["report"]["status"] == "draft"
            and "ai_draft" not in r["report"]
        )
        assert (await c.get(f"/reports/zhangsan/{DAY}", headers=lisi)).status_code == 404  # draft: invisible
        assert (await c.post(f"/reports/{rid}/confirm", headers=lisi)).status_code == 403  # author only
        assert (await c.post(f"/reports/{rid}/confirm", headers=z)).json()["status"] == "confirmed"
        assert (await c.post(f"/reports/{rid}/confirm", headers=z)).status_code == 422
        seen = (await c.get(f"/reports/zhangsan/{DAY}", headers=lisi)).json()
        assert seen["access"] == "full" and seen["raw_input"] is None
        assert (await c.get(f"/reports/zhangsan/{DAY}", headers=boss)).status_code == 403  # skip level
        assert len((await c.get("/reports/zhangsan/history", headers=lisi)).json()) == 1
        team = (await c.get(f"/team?date={DAY}", headers=lisi)).json()
        assert [m["user"]["id"] for m in team["members"] if m["report"]] == ["zhangsan"]
        assert (await c.get(f"/team?date={DAY}", headers=z)).status_code == 403


async def test_rollup_over_http(org):
    await submit(org.reports, "zhangsan", DAY, [item("done", "x")])
    lisi = await token_for(org, "lisi")
    a = FakeAdapter({"e1": [resp(json.dumps(ROLLUP, ensure_ascii=False))]})
    async with make_client(org, a, api_token="svc", env="prod") as c:
        d = (await c.post("/team/rollup", json={"date": DAY}, headers=lisi)).json()
        assert d["report"]["team_summary"]["stats"]["submitted"] == 1 and d["warnings"]
        rid = d["report"]["id"]
        bad = await c.post(f"/team/rollup/{rid}/confirm", json={"escalate": [9]}, headers=lisi)
        assert bad.status_code == 422
        ok = (await c.post(f"/team/rollup/{rid}/confirm", json={"escalate": [1]}, headers=lisi)).json()
        assert ok["report"]["team_summary"]["escalations"][0]["text"] == "e"


@pytest.mark.parametrize("path", ["/team/reviews", "/team/rollup"])
async def test_manager_endpoints_reject_members(org, path):
    z = await token_for(org, "zhangsan")
    async with make_client(org, FakeAdapter(), api_token="svc", env="prod") as c:
        body = {"member": "wangwu", "date": DAY} if "reviews" in path else {"date": DAY}
        assert (await c.post(path, json=body, headers=z)).status_code == 403
