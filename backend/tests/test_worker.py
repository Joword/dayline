"""Worker on the staggered per-team schedule (MongoDB + scripted model)."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.bootstrap import Services
from app.core.config import Settings
from app.db.repositories.escalations import EscalationsRepo, NotificationsRepo
from app.escalations.service import EscalationService
from app.jobs.schedule import ScheduleConfig
from app.jobs.worker import Worker
from app.notify.service import Notifier
from tests.conftest import FakeAdapter, item, resp, submit
from tests.test_manager_flows import ROLLUP, service

TZ = ZoneInfo("Asia/Shanghai")
DAY = "2026-11-17"


class FakeMongo:
    def __init__(self, db):
        self.db = db


def services(org, adapter: FakeAdapter) -> Services:
    svc = service(org, adapter)
    notifications = NotificationsRepo(org.db)
    notifier = Notifier(notifications)
    svc.notifier = notifier
    svc.escalations = EscalationService(
        repo=EscalationsRepo(org.db), users=org.users, relay=org.relay, notifier=notifier
    )
    return Services(
        settings=Settings(),
        gateway=svc.runner.gateway,
        tools=svc.runner.tools,
        rules=svc.runner.rules,
        registry=svc.runner.registry,
        router=None,  # type: ignore[arg-type]
        runner=svc.runner,
        mongo=FakeMongo(org.db),  # type: ignore[arg-type]
        reports=org.reports,
        users=org.users,
        reviews=org.reviews,
        audit=org.audit,
        relay=org.relay,
        report_service=svc,
        notifications=notifications,
        notifier=notifier,
        escalations=svc.escalations,
    )


def at(hh: int, mm: int):
    return lambda: datetime(2026, 11, 17, hh, mm, tzinfo=TZ)


SCHEDULE = ScheduleConfig.model_validate(
    {"default": {"deadline": "18:00", "rollup": "18:30"}, "level_gap_minutes": 60}
)


async def test_staggered_plan_by_level(org):
    w = Worker(services(org, FakeAdapter()), schedule=SCHEDULE)
    plan = {(m.id, job): t for m, job, t in await w.plan()}
    # lisi / qianqi manage individual contributors; boss manages managers -> one level later
    assert plan[("lisi", "deadline")] == "18:00" and plan[("lisi", "rollup")] == "18:30"
    assert plan[("boss", "deadline")] == "19:00" and plan[("boss", "rollup")] == "19:30"
    assert ("zhangsan", "deadline") not in plan


async def test_org_unit_override(org):
    cfg = ScheduleConfig.model_validate({"org_units": {"风控组": {"deadline": "17:30", "rollup": "18:00"}}})
    plan = {(m.id, job): t for m, job, t in await Worker(services(org, FakeAdapter()), schedule=cfg).plan()}
    assert plan[("qianqi", "deadline")] == "17:30" and plan[("lisi", "deadline")] == "18:00"


async def test_jobs_run_once_per_team_at_their_time_and_notify(org):
    await submit(org.reports, "zhangsan", DAY, [item("done", "x")])
    a = FakeAdapter({"e1": [resp(json.dumps(ROLLUP, ensure_ascii=False))]})  # one roll-up: lisi only
    s = services(org, a)
    assert await Worker(s, schedule=SCHEDULE, now=at(17, 59)).run_due() == []
    ran = await Worker(s, schedule=SCHEDULE, now=at(18, 10)).run_due()
    assert sorted(ran) == ["deadline:lisi", "deadline:qianqi"]  # boss's team is due at 19:00
    for uid in ("wangwu", "zhaoliu", "sunba"):
        notes = await s.notifications.for_user(uid)  # type: ignore[union-attr]
        assert [n["kind"] for n in notes] == ["report.missing"]
    assert await s.notifications.for_user("zhangsan") == []  # type: ignore[union-attr]

    ran = await Worker(s, schedule=SCHEDULE, now=at(18, 31)).run_due()
    assert sorted(ran) == ["rollup:lisi", "rollup:qianqi"]
    runs = {d["_id"]: d async for d in org.db["job_runs"].find()}
    assert runs[f"rollup:qianqi:{DAY}"]["result"] == {"skipped": "nobody submitted"}
    assert (await org.reports.latest("lisi", DAY))["status"] == "draft"  # drafted, never sent
    assert [n["kind"] for n in await s.notifications.for_user("lisi")] == ["rollup.ready"]  # type: ignore[union-attr]

    w = Worker(s, schedule=SCHEDULE, now=at(19, 45))
    assert sorted(await w.run_due()) == ["deadline:boss", "rollup:boss"]
    assert await w.run_due() == []  # never twice a day
    # the deadline reminder is not duplicated by a retry (dedupe key per person per day)
    await s.notifier.notify(["wangwu"], "report.missing", "again", dedupe_key=f"report.missing:{DAY}")  # type: ignore[union-attr]
    assert len(await s.notifications.for_user("wangwu")) == 1  # type: ignore[union-attr]


async def test_manager_who_rolled_up_already_is_skipped(org):
    await submit(org.reports, "zhangsan", DAY, [item("done", "x")])
    s = services(org, FakeAdapter({"e1": [resp(json.dumps(ROLLUP, ensure_ascii=False))]}))
    await s.report_service.draft_rollup("lisi", DAY)  # type: ignore[union-attr]
    await Worker(s, schedule=SCHEDULE, now=at(18, 31)).run_due()
    run = await org.db["job_runs"].find_one({"_id": f"rollup:lisi:{DAY}"})
    assert run["result"] == {"skipped": "already rolled up"}


async def test_single_active_worker(org):
    s = services(org, FakeAdapter())
    w1, w2 = Worker(s, schedule=SCHEDULE), Worker(s, schedule=SCHEDULE)
    assert await w1.acquire_lease() and await w1.acquire_lease()  # renewal
    assert not await w2.acquire_lease()
