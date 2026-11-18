"""skill_runs repository: one document per SkillRunner run (cost, tokens, endpoint, trace)."""

from __future__ import annotations

from typing import Any

from pymongo import DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from app.db.models import SkillRunDoc


class SkillRunsRepo:
    def __init__(self, db: AsyncDatabase):
        self.col = db["skill_runs"]

    async def record(self, run: SkillRunDoc) -> None:
        await self.col.insert_one(run.model_dump())

    async def recent(
        self, *, user_id: str | None = None, skill: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        q: dict[str, Any] = {}
        if user_id:
            q["user_id"] = user_id
        if skill:
            q["skill"] = skill
        cursor = self.col.find(q, {"trace": 0}).sort("created_at", DESCENDING).limit(limit)
        out = []
        async for d in cursor:
            d["id"] = str(d.pop("_id"))
            out.append(d)
        return out
