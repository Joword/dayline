"""Background worker: per-team daily jobs on a staggered schedule (plan §4.5, §12.1).

For every manager, at the times ``app.jobs.schedule`` computes for their team:

  deadline  remind members who have not submitted (in-app now; DingTalk in M6)
  rollup    draft the team roll-up and tell the manager it is ready - never sent automatically

Every tick it also delivers the outbox, and every ``tools_verify_minutes`` it checks that
enabled member tools still serve the definition that was approved (M7).

Run with ``dl worker``. Single active instance: a lease in ``locks`` keeps a second
worker idle. Each (job, manager, day) runs at most once: ``job_runs`` holds one document
per key, inserted before the job starts, so a restart cannot repeat it.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pymongo.errors import DuplicateKeyError

from app.bootstrap import Services
from app.db.models import UserDoc, utcnow
from app.jobs.schedule import ScheduleConfig, heights, load_schedule, team_times

log = logging.getLogger("dayline.worker")

LEASE_S = 90

JobFn = Callable[[Services, UserDoc, str], Awaitable[dict[str, Any]]]


async def deadline_job(s: Services, manager: UserDoc, day: str) -> dict[str, Any]:
    snap = await s.relay.team_snapshot(await s.relay.viewer(manager.id, client_id="worker"), day)  # type: ignore[union-attr]
    missing = [u.id for u in snap.missing]
    if missing and s.notifier is not None:
        await s.notifier.notify(
            missing,
            "report.missing",
            f"{day} 的日报还没有提交",
            body="写好后确认提交,上级才能看到。",
            link="/generate",
            dedupe_key=f"report.missing:{day}",
        )
    log.info("deadline reached", extra={"manager": manager.id, "date": day, "missing": missing})
    return {"missing": missing}


async def rollup_job(s: Services, manager: UserDoc, day: str) -> dict[str, Any]:
    latest = await s.reports.latest(manager.id, day)  # type: ignore[union-attr]
    if latest and latest.get("team_summary"):
        return {"skipped": "already rolled up"}
    snap = await s.relay.team_snapshot(await s.relay.viewer(manager.id, client_id="worker"), day)  # type: ignore[union-attr]
    if not snap.submitted:
        return {"skipped": "nobody submitted"}
    result, doc, _ = await s.report_service.draft_rollup(manager.id, day)  # type: ignore[union-attr]
    if doc is None:
        return {"failed": result.errors}
    if s.notifier is not None:
        await s.notifier.notify(
            [manager.id],
            "rollup.ready",
            f"{day} 的团队汇总草稿已生成",
            body=f"已提交 {len(snap.submitted)}/{len(snap.members)},请确认后发送给上级。",
            link=f"/team?date={day}",
            dedupe_key=f"rollup.ready:{day}",
        )
    return {"drafted": doc["id"]}


JOBS: dict[str, JobFn] = {"deadline": deadline_job, "rollup": rollup_job}


class Worker:
    def __init__(
        self,
        services: Services,
        *,
        schedule: ScheduleConfig | None = None,
        now: Callable[[], datetime] | None = None,
        tick_s: float = 30.0,
    ):
        st = services.settings
        if services.mongo is None or services.relay is None:
            raise RuntimeError("the worker needs MongoDB")
        self.s = services
        self.tz = ZoneInfo(st.timezone)
        self.schedule = schedule or load_schedule(
            st.resolve(st.schedule_config), deadline=st.deadline_time, rollup=st.rollup_draft_time
        )
        self.now = now or (lambda: datetime.now(self.tz))
        self.tick_s = tick_s
        self.holder = f"{socket.gethostname()}:{uuid.uuid4().hex[:6]}"
        db = services.mongo.db
        self.locks, self.runs = db["locks"], db["job_runs"]
        self._tools_checked_at: datetime | None = None

    async def acquire_lease(self) -> bool:
        now = utcnow()
        try:
            doc = await self.locks.find_one_and_update(
                {"_id": "worker", "$or": [{"expires_at": {"$lt": now}}, {"holder": self.holder}]},
                {"$set": {"holder": self.holder, "expires_at": now + timedelta(seconds=LEASE_S)}},
                upsert=True,
                return_document=True,
            )
        except DuplicateKeyError:
            return False  # held by another live worker
        return doc is not None and doc.get("holder") == self.holder

    async def plan(self) -> list[tuple[UserDoc, str, str]]:
        """(manager, job, HH:MM) for every manager in the active org."""
        users = await self.s.users.all()  # type: ignore[union-attr]
        by_id = {u.id: u for u in users}
        out = []
        for mid, h in heights(users).items():
            m = by_id.get(mid)
            if m is None or h == 0:
                continue
            deadline, rollup = team_times(self.schedule, m, h)
            out += [(m, "deadline", deadline), (m, "rollup", rollup)]
        return out

    async def run_due(self) -> list[str]:
        now = self.now()
        day, hhmm = now.date().isoformat(), now.strftime("%H:%M")
        ran = []
        for manager, job, at in await self.plan():
            if hhmm < at:
                continue
            key = f"{job}:{manager.id}:{day}"
            try:
                await self.runs.insert_one(
                    {
                        "_id": key,
                        "job": job,
                        "manager": manager.id,
                        "date": day,
                        "at": at,
                        "status": "running",
                        "started_at": utcnow(),
                        "holder": self.holder,
                    }
                )
            except DuplicateKeyError:
                continue  # already ran (or is running) today
            try:
                result = await JOBS[job](self.s, manager, day)
                update: dict[str, Any] = {"status": "ok", "result": result}
            except Exception as e:
                log.exception("job failed", extra={"job": job, "manager": manager.id})
                update = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            await self.runs.update_one({"_id": key}, {"$set": {**update, "finished_at": utcnow()}})
            ran.append(f"{job}:{manager.id}")
        return ran

    async def verify_tools(self) -> list[tuple[str, str]]:
        """Every ``tools_verify_minutes``: enabled member tools must still match their approval."""
        svc = self.s.tool_service
        if svc is None or svc.connector is None:
            return []
        now = self.now()
        interval = timedelta(minutes=self.s.settings.tools_verify_minutes)
        if self._tools_checked_at is not None and now - self._tools_checked_at < interval:
            return []
        self._tools_checked_at = now
        results = await svc.verify()
        if results:
            log.info("tools verified", extra={"results": results})
        return results

    async def process_outbox(self) -> list[tuple[str, str]]:
        """Deliver due outbox items (DingTalk logs and messages); runs every tick."""
        if self.s.outbox is None or not self.s.outbox.enabled:
            return []
        return await self.s.outbox.process_due()

    async def run_forever(self) -> None:
        log.info(
            "worker started",
            extra={"holder": self.holder, "level_gap_minutes": self.schedule.level_gap_minutes},
        )
        while True:
            if await self.acquire_lease():
                await self.run_due()
                try:
                    await self.process_outbox()
                except Exception:
                    log.exception("outbox processing failed")
                try:
                    await self.verify_tools()
                except Exception:
                    log.exception("tool verification failed")
            await asyncio.sleep(self.tick_s)
