"""reviews and audit_logs repositories."""

from __future__ import annotations

import re
from typing import Any

from pymongo import DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from app.db.models import AuditDoc, ReviewDoc


class ReviewsRepo:
    def __init__(self, db: AsyncDatabase):
        self.col = db["reviews"]

    async def insert(self, doc: ReviewDoc) -> str:
        res = await self.col.insert_one(doc.model_dump())
        return str(res.inserted_id)

    async def for_subject(
        self, subject_id: str, *, reviewer_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        cursor = (
            self.col.find({"subject_id": subject_id, "reviewer_id": reviewer_id})
            .sort("created_at", DESCENDING)
            .limit(limit)
        )
        out = []
        async for d in cursor:
            d["id"] = str(d.pop("_id"))
            out.append(d)
        return out


class AuditRepo:
    def __init__(self, db: AsyncDatabase):
        self.col = db["audit_logs"]

    async def write(self, doc: AuditDoc) -> None:
        await self.col.insert_one(doc.model_dump())

    async def recent(self, *, actor_id: str | None = None, target_prefix: str | None = None, limit: int = 50):
        q: dict[str, Any] = {}
        if actor_id:
            q["actor_id"] = actor_id
        if target_prefix:
            q["target"] = {"$regex": "^" + re.escape(target_prefix)}
        cursor = self.col.find(q, {"_id": 0}).sort("ts", DESCENDING).limit(limit)
        return [d async for d in cursor]
