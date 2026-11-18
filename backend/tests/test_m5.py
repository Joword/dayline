"""M5: escalation chain, roll-up integration, snapshot change alerts, schedule maths."""

from __future__ import annotations

import json

import pytest

from app.db.models import EscalationOrigin
from app.escalations.service import EscalationError, EscalationForbidden
from app.jobs.schedule import ScheduleConfig, _shift, heights, team_times
from app.org.importer import apply_plan, build_plan
from app.relay.service import RelayForbidden
from tests.conftest import ORG_ROWS, FakeAdapter, item, resp, submit
from tests.test_manager_flows import ROLLUP
from tests.test_worker import services

DAY = "2026-11-17"
BLOCKERS = [item("done", "上线重试"), item("blocker", "等风控接口文档", severity="high", need_from="风控组")]


@pytest.fixture
async def org4(org):
    """ceo -> boss -> lisi -> zhangsan: four levels, so an item can travel all the way up."""
    rows = [{"id": "ceo", "name": "钱总"}] + [
        dict(r, manager="ceo") if r["id"] == "boss" else dict(r) for r in ORG_ROWS
    ]
    await apply_plan(org.users, build_plan(rows))
    return org


async def test_escalation_travels_up_and_is_tracked(org4):
    s = services(org4, FakeAdapter())
    esc = s.escalations
    await submit(org4.reports, "zhangsan", DAY, BLOCKERS)

    e = await esc.raise_from_report("lisi", "zhangsan", DAY, 1, note="跨团队,一周没进展")
    assert (e.status, e.current_holder_id, e.severity, e.need_from) == ("escalated", "boss", "high", "风控组")
    assert e.origin.report_version == 1 and set(e.participants) == {"lisi", "zhangsan", "boss"}
    assert [n["kind"] for n in await s.notifications.for_user("boss")] == ["escalation.escalated"]

    with pytest.raises(EscalationError, match="already raised"):
        await esc.raise_from_report("lisi", "zhangsan", DAY, 1)
    with pytest.raises(EscalationError, match="only blockers"):
        await esc.raise_from_report("lisi", "zhangsan", DAY, 0)
    with pytest.raises(EscalationForbidden):
        await esc.accept("lisi", e.id)  # lisi no longer holds it

    e = await esc.accept("boss", e.id, note="我来协调")
    assert e.status == "in_progress"
    e = await esc.escalate("boss", e.id, note="需要钱总和风控负责人沟通")
    assert (e.status, e.current_holder_id) == ("escalated", "ceo") and "ceo" in e.participants
    await esc.accept("ceo", e.id)
    with pytest.raises(EscalationError, match="no manager"):
        await esc.escalate("ceo", e.id)  # nobody above the CEO
    with pytest.raises(EscalationError, match="note is required"):
        await esc.resolve("ceo", e.id, " ")
    e = await esc.resolve("ceo", e.id, "已和风控负责人约定周三前给文档")
    assert e.status == "resolved" and e.resolution.startswith("已和风控")
    assert [ev.action for ev in e.chain] == [
        "created",
        "escalated",
        "accepted",
        "escalated",
        "accepted",
        "resolved",
    ]
    assert [(ev.from_holder, ev.to_holder) for ev in e.chain if ev.action == "escalated"] == [
        ("lisi", "boss"),
        ("boss", "ceo"),
    ]
    # everyone in the chain, including the member, hears about the resolution
    for uid in ("zhangsan", "lisi", "boss"):
        assert "escalation.resolved" in {n["kind"] for n in await s.notifications.for_user(uid)}


async def test_escalation_visibility_and_permissions(org4):
    s = services(org4, FakeAdapter())
    await submit(org4.reports, "zhangsan", DAY, BLOCKERS)
    e = await s.escalations.raise_from_report("lisi", "zhangsan", DAY, 1)
    for outsider in ("wangwu", "qianqi", "ceo"):
        with pytest.raises(EscalationForbidden):
            await s.escalations.get(outsider, e.id)
    assert (await s.escalations.get("zhangsan", e.id)).id == e.id  # the member can follow their blocker
    with pytest.raises(RelayForbidden):
        await s.escalations.raise_from_report("qianqi", "zhangsan", DAY, 1)  # not their report
    with pytest.raises(EscalationForbidden):
        await s.escalations.raise_item(
            "qianqi", text="x", origin=EscalationOrigin(user_id="zhangsan", date=DAY)
        )
    c = await s.escalations.comment("zhangsan", e.id, "文档负责人是钱七组的孙八")
    assert c.chain[-1].action == "commented" and c.status == "escalated"
    assert [x.id for x in await s.escalations.find_for("boss", "held")] == [e.id]
    assert [x.id for x in await s.escalations.find_for("lisi", "raised")] == [e.id]
    assert await s.escalations.find_for("wangwu", "involved") == []


async def test_concurrent_transition_has_one_winner(org4):
    s = services(org4, FakeAdapter())
    e = await s.escalations.raise_item("lisi", text="t", origin=EscalationOrigin(user_id="lisi", date=DAY))
    first = await s.escalations.repo.transition(
        e.id,
        expect_holder="boss",
        expect_status=["escalated"],
        event=e.chain[0],
        set_={"status": "in_progress"},
    )
    second = await s.escalations.repo.transition(
        e.id, expect_holder="boss", expect_status=["escalated"], event=e.chain[0], set_={"status": "resolved"}
    )
    assert first is not None and second is None


async def test_rollup_raises_forwards_and_carries_escalations(org4):
    await submit(org4.reports, "zhangsan", DAY, BLOCKERS)
    await submit(org4.reports, "lisi", DAY, [item("done", "评审方案")])
    boss_rollup = {
        "summary": "两个组按计划推进。",
        "highlights": [],
        "risks": [],
        "escalation_candidates": [],
    }
    a = FakeAdapter(
        {
            "e1": [
                resp(json.dumps(ROLLUP, ensure_ascii=False)),
                resp(json.dumps(boss_rollup, ensure_ascii=False)),
            ]
        }
    )
    s = services(org4, a)
    # lisi raised zhangsan's blocker during the day (e.g. while reviewing)
    early = await s.escalations.raise_from_report("lisi", "zhangsan", DAY, 1)

    _, draft, _ = await s.report_service.draft_rollup("lisi", DAY)
    ts = draft["team_summary"]
    assert [e["escalation_id"] for e in ts["raised_escalations"]] == [early.id]
    confirmed, _ = await s.report_service.confirm_rollup("lisi", draft["id"], escalate=[2])
    escs = confirmed["team_summary"]["escalations"]
    assert {e["text"] for e in escs} == {"等风控接口文档", "测试环境不稳定"}  # raised today + newly picked
    assert all(e["status"] == "escalated" and e["holder"] in ("boss", "赵总") for e in escs)
    assert "escalation_candidates" not in confirmed["team_summary"]

    # boss sees both waiting for them and forwards one further up when rolling up
    await submit(org4.reports, "qianqi", DAY, [item("done", "y")])
    _, boss_draft, _ = await s.report_service.draft_rollup("boss", DAY)
    pending = boss_draft["team_summary"]["pending_escalations"]
    assert len(pending) == 2 and "收到的升级事项" in boss_draft["rendered_md"]
    fwd = next(p["escalation_id"] for p in pending if p["text"] == "等风控接口文档")
    with pytest.raises(Exception, match="not waiting for you"):
        await s.report_service.confirm_rollup("boss", boss_draft["id"], forward=["nope"])
    done, _ = await s.report_service.confirm_rollup("boss", boss_draft["id"], forward=[fwd])
    assert [e["escalation_id"] for e in done["team_summary"]["escalations"]] == [fwd]
    moved = await s.escalations.get("ceo", fwd)
    assert moved.current_holder_id == "ceo" and [ev.action for ev in moved.chain][-1] == "escalated"


async def test_member_resubmitting_after_rollup_alerts_manager(org4):
    await submit(org4.reports, "zhangsan", DAY, [item("done", "x")])
    s = services(org4, FakeAdapter({"e1": [resp(json.dumps(ROLLUP, ensure_ascii=False))]}))
    await s.report_service.draft_rollup("lisi", DAY)
    newer = await submit(org4.reports, "zhangsan", DAY, [item("done", "x 和 y")], confirm=False)
    await s.report_service.confirm("zhangsan", newer["id"])
    notes = await s.notifications.for_user("lisi")
    assert [n["kind"] for n in notes] == ["report.changed"] and "v1 → v2" in notes[0]["title"]
    await s.report_service.confirm(
        "wangwu", (await submit(org4.reports, "wangwu", DAY, [item("done", "z")], confirm=False))["id"]
    )
    assert len(await s.notifications.for_user("lisi")) == 1  # wangwu was not in the roll-up: no alert


async def test_notifications_read_state(org4):
    s = services(org4, FakeAdapter())
    await s.notifier.notify(["lisi"], "x", "one")
    await s.notifier.notify(["lisi"], "x", "two")
    assert await s.notifications.unread_count("lisi") == 2
    first = (await s.notifications.for_user("lisi"))[0]["id"]
    assert await s.notifications.mark_read("lisi", [first]) == 1
    assert await s.notifications.mark_read("wangwu") == 0  # someone else's
    assert await s.notifications.unread_count("lisi") == 1 and await s.notifications.mark_read("lisi") == 1


def test_schedule_maths():
    from app.db.models import UserDoc

    def u(uid, mgr=None, unit=None):
        return UserDoc(id=uid, name=uid, manager_id=mgr, org_path=f"/{uid}/", org_unit=unit)

    users = [u("ceo"), u("boss", "ceo"), u("lisi", "boss", "支付组"), u("zs", "lisi"), u("solo", "ceo")]
    assert heights(users) == {"ceo": 3, "boss": 2, "lisi": 1}
    cfg = ScheduleConfig.model_validate(
        {"level_gap_minutes": 45, "org_units": {"支付组": {"deadline": "18:30", "rollup": "19:00"}}}
    )
    assert team_times(cfg, users[2], 1) == ("18:30", "19:00")
    assert team_times(cfg, users[0], 3) == ("19:30", "20:00")
    assert _shift("23:30", 120) == "23:59"
    with pytest.raises(ValueError):
        ScheduleConfig.model_validate({"default": {"deadline": "19:00", "rollup": "18:00"}})


async def test_escalation_and_notification_api(org4):
    import httpx

    from app.api.main import create_app
    from tests.test_api_org import token_for

    await submit(org4.reports, "zhangsan", DAY, BLOCKERS)
    s = services(org4, FakeAdapter())
    s.settings = s.settings.model_copy(update={"api_token": None, "env": "prod"})
    app = create_app(s.settings, services=s)
    app.state.services = s
    lisi = await token_for(org4, "lisi")
    boss = await token_for(org4, "boss")
    wangwu = await token_for(org4, "wangwu")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/escalations",
            json={"member": "zhangsan", "date": DAY, "item_index": 1, "note": "急"},
            headers=lisi,
        )
        assert r.status_code == 200, r.text
        esc_id = r.json()["id"]
        assert (
            await c.post(
                "/escalations", json={"member": "zhangsan", "date": DAY, "item_index": 0}, headers=lisi
            )
        ).status_code == 422
        held = (await c.get("/escalations?scope=held", headers=boss)).json()
        assert [e["id"] for e in held] == [esc_id]
        assert (await c.get(f"/escalations/{esc_id}", headers=wangwu)).status_code == 403
        assert (await c.post(f"/escalations/{esc_id}/accept", json={}, headers=lisi)).status_code == 403
        assert (await c.post(f"/escalations/{esc_id}/accept", json={"note": "我来"}, headers=boss)).json()[
            "status"
        ] == "in_progress"
        assert (
            await c.post(f"/escalations/{esc_id}/resolve", json={}, headers=boss)
        ).status_code == 422  # note required
        done = (await c.post(f"/escalations/{esc_id}/resolve", json={"note": "已解决"}, headers=boss)).json()
        assert done["status"] == "resolved"
        n = (await c.get("/me/notifications?unread=true", headers=lisi)).json()
        assert n["unread"] >= 2 and {i["kind"] for i in n["items"]} >= {
            "escalation.accepted",
            "escalation.resolved",
        }
        assert (await c.post("/me/notifications/read", json={}, headers=lisi)).json()["marked"] == n["unread"]
        assert (await c.get("/me/notifications", headers=lisi)).json()["unread"] == 0
