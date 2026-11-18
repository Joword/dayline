"""Reports repository. Skills and tools never touch the collection directly
(and since M2 they read through Relay, which calls this repository).

Every save creates a new version document (versions are immutable snapshots, so a
manager's roll-up can cite ``(report_id, version)``). Confirming marks the latest
version as the submitted one.
"""

from __future__ import annotations

from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import DESCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.db.models import SUBMITTED, ReportDoc, SkillRef, utcnow


def _out(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if doc is not None:
        doc["id"] = str(doc.pop("_id"))
    return doc


def _oid(report_id: str) -> ObjectId | None:
    try:
        return ObjectId(report_id)
    except (InvalidId, TypeError):
        return None


class ReportConflict(Exception):
    pass


class ReportsRepo:
    def __init__(self, db: AsyncDatabase):
        self.col = db["reports"]

    async def latest(self, user_id: str, date: str) -> dict[str, Any] | None:
        return _out(
            await self.col.find_one({"user_id": user_id, "date": date}, sort=[("version", DESCENDING)])
        )

    async def latest_submitted(self, user_id: str, date: str) -> dict[str, Any] | None:
        return _out(
            await self.col.find_one(
                {"user_id": user_id, "date": date, "status": {"$in": list(SUBMITTED)}},
                sort=[("version", DESCENDING)],
            )
        )

    async def latest_submitted_many(self, user_ids: list[str], date: str) -> dict[str, dict[str, Any]]:
        """{user_id: latest submitted version for `date`} for the given users."""
        pipeline = [
            {"$match": {"user_id": {"$in": user_ids}, "date": date, "status": {"$in": list(SUBMITTED)}}},
            {"$sort": {"version": -1}},
            {"$group": {"_id": "$user_id", "doc": {"$first": "$$ROOT"}}},
            {"$replaceRoot": {"newRoot": "$doc"}},
        ]
        cursor = await self.col.aggregate(pipeline)
        return {d["user_id"]: _out(d) async for d in cursor}  # type: ignore[misc]

    async def get(self, report_id: str) -> dict[str, Any] | None:
        oid = _oid(report_id)
        return _out(await self.col.find_one({"_id": oid})) if oid else None

    async def save_draft(
        self,
        *,
        user_id: str,
        date: str,
        lang: str,
        raw_input: dict[str, Any],
        output: dict[str, Any],
        rendered_md: str,
        skill_name: str,
        skill_version: str,
        skill_run_id: str | None,
        kind: str = "individual",
        carry_team_summary: bool = True,
    ) -> dict[str, Any]:
        """Store a new version. A manager who rewrites their own items after the roll-up
        keeps the team summary (``carry_team_summary``) - the newest version stays complete."""
        for _ in range(5):  # version race between two writers: retry with the next number
            prev = await self.latest(user_id, date)
            team_summary = output.get("team_summary")
            if team_summary is None and carry_team_summary and prev and prev.get("team_summary"):
                team_summary = prev["team_summary"]
            out = {**output, "team_summary": team_summary} if team_summary is not None else output
            doc = ReportDoc(
                user_id=user_id,
                date=date,
                kind="manager" if team_summary is not None else kind,  # type: ignore[arg-type]
                version=(prev["version"] + 1) if prev else 1,
                lang=lang,
                raw_input=raw_input,
                own_items=out.get("own_items", []),
                team_summary=team_summary,
                skill=SkillRef(name=skill_name, version=skill_version),
                skill_run_id=skill_run_id,
                output=out,
                rendered_md=rendered_md,
                ai_draft=output,
            ).model_dump()
            try:
                res = await self.col.insert_one(doc)
            except DuplicateKeyError:
                continue
            doc["_id"] = res.inserted_id
            return _out(doc)  # type: ignore[return-value]
        raise RuntimeError("could not allocate a report version")

    async def confirm(
        self, report_id: str, *, output: dict[str, Any] | None = None, rendered_md: str | None = None
    ) -> dict[str, Any] | None:
        """Confirm a draft. Only the newest version of that day can be confirmed."""
        current = await self.get(report_id)
        if current is None or current["status"] != "draft":
            return None
        newest = await self.latest(current["user_id"], current["date"])
        if newest and newest["id"] != report_id:
            raise ReportConflict(f"version {current['version']} is outdated; v{newest['version']} exists")
        update: dict[str, Any] = {"status": "confirmed", "confirmed_at": utcnow(), "updated_at": utcnow()}
        if output is not None:
            update["output"] = output
            update["own_items"] = output.get("own_items", [])
            update["team_summary"] = output.get("team_summary", current.get("team_summary"))
        if rendered_md is not None:
            update["rendered_md"] = rendered_md
        return _out(
            await self.col.find_one_and_update(
                {"_id": ObjectId(report_id), "status": "draft"},
                {"$set": update},
                return_document=ReturnDocument.AFTER,
            )
        )

    async def list_recent(
        self, user_id: str, *, before: Any = None, since: Any = None, limit: int = 3
    ) -> list[dict[str, Any]]:
        """Latest submitted version per day, newest day first."""
        match: dict[str, Any] = {"user_id": user_id, "status": {"$in": list(SUBMITTED)}}
        rng: dict[str, str] = {}
        if before is not None:
            rng["$lt"] = str(before)
        if since is not None:
            rng["$gte"] = str(since)
        if rng:
            match["date"] = rng
        pipeline = [
            {"$match": match},
            {"$sort": {"date": -1, "version": -1}},
            {"$group": {"_id": "$date", "doc": {"$first": "$$ROOT"}}},
            {"$replaceRoot": {"newRoot": "$doc"}},
            {"$sort": {"date": -1}},
            {"$limit": limit},
        ]
        cursor = await self.col.aggregate(pipeline)
        return [_out(d) async for d in cursor]  # type: ignore[misc]

    async def set_sync(
        self,
        report_id: str,
        *,
        status: str,
        dingtalk_log_id: str | None = None,
        last_error: str | None = None,
    ) -> None:
        """DingTalk log sync state (plan §10: reports.sync)."""
        oid = _oid(report_id)
        if oid is None:
            return
        update: dict[str, Any] = {
            "sync.status": status,
            "sync.last_error": last_error,
            "updated_at": utcnow(),
        }
        if dingtalk_log_id is not None:
            update["sync.dingtalk_log_id"] = dingtalk_log_id
        await self.col.update_one({"_id": oid}, {"$set": update})
