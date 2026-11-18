"""integrations (encrypted credentials) and activity_cache repositories."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.db.models import utcnow


class IntegrationDoc(BaseModel):
    schema_version: int = 1
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    user_id: str
    type: Literal["git", "calendar"]
    provider: str
    label: str = ""
    config_enc: str
    status: Literal["active", "error"] = "active"
    state: dict[str, Any] = Field(default_factory=dict)  # e.g. {"mode_used": "caldav"}
    last_ok_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


def _out(d: dict[str, Any] | None) -> IntegrationDoc | None:
    if d is None:
        return None
    d = dict(d)
    d["id"] = d.pop("_id")
    return IntegrationDoc.model_validate(d)


class IntegrationsRepo:
    def __init__(self, db: AsyncDatabase):
        self.col = db["integrations"]

    async def add(self, doc: IntegrationDoc) -> IntegrationDoc:
        d = doc.model_dump()
        d["_id"] = d.pop("id")
        await self.col.insert_one(d)
        return doc

    async def for_user(self, user_id: str, type_: str | None = None) -> list[IntegrationDoc]:
        q: dict[str, Any] = {"user_id": user_id}
        if type_:
            q["type"] = type_
        return [_out(d) async for d in self.col.find(q).sort("created_at", 1)]  # type: ignore[misc]

    async def users_with(self, type_: str | None = None) -> list[str]:
        return await self.col.distinct("user_id", {"type": type_} if type_ else {})

    async def remove(self, user_id: str, integration_id: str) -> bool:
        res = await self.col.delete_one({"_id": integration_id, "user_id": user_id})
        return res.deleted_count == 1

    async def record_result(
        self,
        integration_id: str,
        *,
        ok: bool,
        error: str | None = None,
        state: dict[str, Any] | None = None,
        config_enc: str | None = None,
    ) -> None:
        update: dict[str, Any] = {
            "status": "active" if ok else "error",
            "updated_at": utcnow(),
            "last_error": error,
        }
        if ok:
            update["last_ok_at"] = utcnow()
        if state is not None:
            update["state"] = state
        if config_enc is not None:  # e.g. a rotated OAuth refresh token
            update["config_enc"] = config_enc
        await self.col.update_one({"_id": integration_id}, {"$set": update})


class ActivityCacheRepo:
    def __init__(self, db: AsyncDatabase):
        self.col = db["activity_cache"]

    async def get(self, user_id: str, date: str, source: str) -> dict[str, Any] | None:
        return await self.col.find_one({"user_id": user_id, "date": date, "source": source}, {"_id": 0})

    async def put(
        self, user_id: str, date: str, source: str, payload: dict[str, Any], *, origin: str = "api"
    ) -> dict[str, Any]:
        return await self.col.find_one_and_update(
            {"user_id": user_id, "date": date, "source": source},
            {"$set": {"payload": payload, "fetched_at": utcnow(), "origin": origin}},
            upsert=True,
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )
