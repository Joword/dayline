"""tools repository (member MCP servers, M7)."""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.db.models import ToolDoc, ToolEvent, utcnow


def _tool(d: dict[str, Any] | None) -> ToolDoc | None:
    if d is None:
        return None
    d = dict(d)
    d["name"] = d.pop("_id")
    return ToolDoc.model_validate(d)


class ToolsRepo:
    def __init__(self, db: AsyncDatabase):
        self.col = db["tools"]

    async def get(self, name: str) -> ToolDoc | None:
        return _tool(await self.col.find_one({"_id": name}))

    async def list(self, statuses: list[str] | None = None) -> list[ToolDoc]:
        q: dict[str, Any] = {"status": {"$in": statuses}} if statuses else {}
        return [_tool(d) async for d in self.col.find(q).sort("_id", ASCENDING)]  # type: ignore[misc]

    async def upsert_submission(self, doc: ToolDoc, event: ToolEvent) -> ToolDoc:
        """Store a (re-)submission. Approvals, health and the disable reason start over."""
        d = doc.model_dump(exclude={"history", "created_at"})
        d["_id"] = d.pop("name")
        d["updated_at"] = utcnow()
        res = await self.col.find_one_and_update(
            {"_id": d["_id"]},
            {
                "$set": d,
                "$push": {"history": event.model_dump()},
                "$setOnInsert": {"created_at": utcnow()},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return _tool(res)  # type: ignore[return-value]

    async def transition(
        self,
        name: str,
        *,
        expect_status: list[str],
        event: ToolEvent,
        set_: dict[str, Any] | None = None,
        push_approval: dict[str, Any] | None = None,
        expect_hash: str | None = None,
    ) -> ToolDoc | None:
        """Atomic status change: applies only if status (and definition hash) are still as expected."""
        q: dict[str, Any] = {"_id": name, "status": {"$in": expect_status}}
        if expect_hash is not None:
            q["definition_hash"] = expect_hash
        push: dict[str, Any] = {"history": event.model_dump()}
        if push_approval is not None:
            push["approvals"] = push_approval
        return _tool(
            await self.col.find_one_and_update(
                q,
                {"$set": {**(set_ or {}), "updated_at": utcnow()}, "$push": push},
                return_document=ReturnDocument.AFTER,
            )
        )

    async def set_health(self, name: str, health: dict[str, Any]) -> None:
        await self.col.update_one({"_id": name}, {"$set": {"health": health}})
