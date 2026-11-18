"""escalations and notifications repositories."""

from __future__ import annotations

from typing import Any

from pymongo import DESCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.db.models import EscalationDoc, EscalationEvent, NotificationDoc, utcnow


def _esc(d: dict[str, Any] | None) -> EscalationDoc | None:
    if d is None:
        return None
    d = dict(d)
    d["id"] = d.pop("_id")
    return EscalationDoc.model_validate(d)


class EscalationsRepo:
    def __init__(self, db: AsyncDatabase):
        self.col = db["escalations"]

    async def insert(self, doc: EscalationDoc) -> EscalationDoc:
        d = doc.model_dump()
        d["_id"] = d.pop("id")
        await self.col.insert_one(d)
        return doc

    async def get(self, esc_id: str) -> EscalationDoc | None:
        return _esc(await self.col.find_one({"_id": esc_id}))

    async def transition(
        self,
        esc_id: str,
        *,
        expect_holder: str,
        expect_status: list[str],
        event: EscalationEvent,
        set_: dict[str, Any],
        add_participant: str | None = None,
    ) -> EscalationDoc | None:
        """Atomic state change: only applies if holder and status are still as expected."""
        update: dict[str, Any] = {
            "$set": {**set_, "updated_at": utcnow()},
            "$push": {"chain": event.model_dump()},
        }
        if add_participant:
            update["$addToSet"] = {"participants": add_participant}
        return _esc(
            await self.col.find_one_and_update(
                {"_id": esc_id, "current_holder_id": expect_holder, "status": {"$in": expect_status}},
                update,
                return_document=ReturnDocument.AFTER,
            )
        )

    async def comment(self, esc_id: str, event: EscalationEvent) -> EscalationDoc | None:
        return _esc(
            await self.col.find_one_and_update(
                {"_id": esc_id},
                {"$push": {"chain": event.model_dump()}, "$set": {"updated_at": utcnow()}},
                return_document=ReturnDocument.AFTER,
            )
        )

    async def find(self, q: dict[str, Any], limit: int = 100) -> list[EscalationDoc]:
        cursor = self.col.find(q).sort("updated_at", DESCENDING).limit(limit)
        return [_esc(d) async for d in cursor]  # type: ignore[misc]


class NotificationsRepo:
    def __init__(self, db: AsyncDatabase):
        self.col = db["notifications"]

    async def add(self, doc: NotificationDoc) -> bool:
        try:
            await self.col.insert_one(doc.model_dump())
        except DuplicateKeyError:
            return False  # already notified for this dedupe_key
        return True

    async def for_user(
        self, user_id: str, *, unread_only: bool = False, limit: int = 50
    ) -> list[dict[str, Any]]:
        q: dict[str, Any] = {"user_id": user_id}
        if unread_only:
            q["read_at"] = None
        out = []
        async for d in self.col.find(q).sort("created_at", DESCENDING).limit(limit):
            d["id"] = str(d.pop("_id"))
            out.append(d)
        return out

    async def unread_count(self, user_id: str) -> int:
        return await self.col.count_documents({"user_id": user_id, "read_at": None})

    async def mark_read(self, user_id: str, ids: list[str] | None = None) -> int:
        from bson import ObjectId
        from bson.errors import InvalidId

        q: dict[str, Any] = {"user_id": user_id, "read_at": None}
        if ids is not None:
            try:
                q["_id"] = {"$in": [ObjectId(i) for i in ids]}
            except (InvalidId, TypeError):
                return 0
        res = await self.col.update_many(q, {"$set": {"read_at": utcnow()}})
        return res.modified_count
