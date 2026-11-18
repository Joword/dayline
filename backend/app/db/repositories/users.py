"""Users / org tree repository."""

from __future__ import annotations

import re
from typing import Any

from pymongo import ReplaceOne
from pymongo.asynchronous.database import AsyncDatabase

from app.db.models import ApiToken, Consent, UserDoc, utcnow


def _in(doc: UserDoc) -> dict[str, Any]:
    d = doc.model_dump()
    d["_id"] = d.pop("id")
    return d


def _out(d: dict[str, Any] | None) -> UserDoc | None:
    if d is None:
        return None
    d = dict(d)
    d["id"] = d.pop("_id")
    return UserDoc.model_validate(d)


class UsersRepo:
    def __init__(self, db: AsyncDatabase):
        self.col = db["users"]

    async def get(self, user_id: str, *, include_inactive: bool = False) -> UserDoc | None:
        q: dict[str, Any] = {"_id": user_id}
        if not include_inactive:
            q["active"] = True
        return _out(await self.col.find_one(q))

    async def get_many(self, ids: list[str]) -> dict[str, UserDoc]:
        return {u.id: u async for u in self._iter({"_id": {"$in": ids}})}

    async def all(self, *, include_inactive: bool = False) -> list[UserDoc]:
        q = {} if include_inactive else {"active": True}
        return [u async for u in self._iter(q, sort=[("org_path", 1)])]

    async def direct_reports(self, manager_id: str) -> list[UserDoc]:
        return [u async for u in self._iter({"manager_id": manager_id, "active": True}, sort=[("name", 1)])]

    async def has_reports(self, user_id: str) -> bool:
        return await self.col.count_documents({"manager_id": user_id, "active": True}, limit=1) > 0

    async def managers(self) -> list[UserDoc]:
        ids = await self.col.distinct("manager_id", {"active": True, "manager_id": {"$ne": None}})
        return [u async for u in self._iter({"_id": {"$in": ids}, "active": True})]

    async def subtree(self, user_id: str) -> list[UserDoc]:
        """The user and everyone below them (prefix query on the materialised path)."""
        me = await self.get(user_id)
        if me is None:
            return []
        return [
            u
            async for u in self._iter({"org_path": {"$regex": "^" + re.escape(me.org_path)}, "active": True})
        ]

    async def _iter(self, q: dict[str, Any], sort: list | None = None):
        cursor = self.col.find(q)
        if sort:
            cursor = cursor.sort(sort)
        async for d in cursor:
            yield _out(d)

    # --- writes -----------------------------------------------------------------

    async def replace_many(self, docs: list[UserDoc]) -> None:
        if docs:
            await self.col.bulk_write([ReplaceOne({"_id": d.id}, _in(d), upsert=True) for d in docs])

    async def deactivate(self, ids: list[str]) -> int:
        if not ids:
            return 0
        res = await self.col.update_many(
            {"_id": {"$in": ids}}, {"$set": {"active": False, "updated_at": utcnow()}}
        )
        return res.modified_count

    async def set_roles(self, user_id: str, roles: list[str]) -> bool:
        res = await self.col.update_one(
            {"_id": user_id}, {"$set": {"system_roles": sorted(set(roles)), "updated_at": utcnow()}}
        )
        return res.matched_count == 1

    async def set_consent(self, user_id: str, consent: Consent) -> None:
        await self.col.update_one({"_id": user_id}, {"$pull": {"consents": {"scope": consent.scope}}})
        await self.col.update_one(
            {"_id": user_id}, {"$push": {"consents": consent.model_dump()}, "$set": {"updated_at": utcnow()}}
        )

    async def remove_consent(self, user_id: str, scope: str) -> bool:
        res = await self.col.update_one(
            {"_id": user_id}, {"$pull": {"consents": {"scope": scope}}, "$set": {"updated_at": utcnow()}}
        )
        return res.modified_count == 1

    async def add_token(self, user_id: str, token: ApiToken) -> None:
        await self.col.update_one({"_id": user_id}, {"$push": {"api_tokens": token.model_dump()}})

    async def find_by_dingtalk_id(self, staff_id: str) -> UserDoc | None:
        return _out(await self.col.find_one({"dingtalk_id": staff_id, "active": True}))

    async def find_by_token_hash(self, token_hash: str) -> UserDoc | None:
        return _out(await self.col.find_one({"api_tokens.hash": token_hash, "active": True}))

    async def touch_token(self, user_id: str, token_id: str) -> None:
        await self.col.update_one(
            {"_id": user_id, "api_tokens.id": token_id}, {"$set": {"api_tokens.$.last_used_at": utcnow()}}
        )

    async def revoke_token(self, user_id: str, token_id: str) -> bool:
        res = await self.col.update_one({"_id": user_id}, {"$pull": {"api_tokens": {"id": token_id}}})
        return res.modified_count == 1
