"""MongoDB client (PyMongo native async)."""

from __future__ import annotations

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import Settings


class Mongo:
    def __init__(self, settings: Settings):
        self.client: AsyncMongoClient = AsyncMongoClient(
            settings.mongo_uri,
            serverSelectionTimeoutMS=settings.mongo_timeout_ms,
            tz_aware=True,
            appname="dayline",
        )
        self.db: AsyncDatabase = self.client[settings.mongo_db]

    async def ping(self) -> bool:
        try:
            await self.client.admin.command("ping")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self.client.close()
