"""Outbox (plan §10): reliable one-way delivery to DingTalk.

Writes happen in our database first (``outbox`` documents), delivery happens later in the
worker, so a DingTalk outage never blocks confirming a report. Each item is claimed under a
lease, retried with backoff on failure, and parked as ``dead`` after ``MAX_ATTEMPTS``
(``dl dingtalk outbox --retry-dead`` re-queues them, e.g. after fixing a template mapping).

    dingtalk.log      a confirmed report -> DingTalk 日志 (rendered from the template mapping)
    dingtalk.message  a notification -> robot one-to-one message
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.db.models import utcnow
from app.db.repositories.reports import ReportsRepo
from app.db.repositories.users import UsersRepo
from app.dingtalk.client import DingTalkClient
from app.dingtalk.templates import render_contents, template_state

log = logging.getLogger("dayline.outbox")

RETRY_S = [60, 300, 900, 3600, 6 * 3600, 12 * 3600]
MAX_ATTEMPTS = len(RETRY_S) + 1
LEASE_S = 120


class PermanentError(Exception):
    """Retrying cannot help (e.g. the user has no DingTalk account)."""


class OutboxRepo:
    def __init__(self, db: AsyncDatabase):
        self.col = db["outbox"]

    async def enqueue(self, type_: str, payload: dict[str, Any], *, dedupe_key: str | None = None) -> bool:
        now = utcnow()
        doc = {
            "type": type_,
            "payload": payload,
            "status": "pending",
            "attempts": 0,
            "next_retry_at": now,
            "lease_until": None,
            "last_error": None,
            "created_at": now,
            "updated_at": now,
        }
        if dedupe_key:
            doc["dedupe_key"] = dedupe_key
        try:
            await self.col.insert_one(doc)
        except DuplicateKeyError:
            return False
        return True

    async def claim(self) -> dict[str, Any] | None:
        now = utcnow()
        return await self.col.find_one_and_update(
            {
                "status": {"$in": ["pending", "processing"]},
                "next_retry_at": {"$lte": now},
                "$or": [{"lease_until": None}, {"lease_until": {"$lt": now}}],
            },
            {
                "$set": {
                    "status": "processing",
                    "lease_until": now + timedelta(seconds=LEASE_S),
                    "updated_at": now,
                }
            },
            sort=[("next_retry_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    async def finish(self, doc_id: Any, *, error: str | None = None, permanent: bool = False) -> str:
        if error is None:
            await self.col.update_one(
                {"_id": doc_id}, {"$set": {"status": "done", "lease_until": None, "updated_at": utcnow()}}
            )
            return "done"
        doc = await self.col.find_one({"_id": doc_id}, {"attempts": 1})
        attempts = (doc or {}).get("attempts", 0) + 1
        dead = permanent or attempts >= MAX_ATTEMPTS
        update = {
            "attempts": attempts,
            "last_error": error,
            "lease_until": None,
            "updated_at": utcnow(),
            "status": "dead" if dead else "pending",
            "next_retry_at": utcnow() + timedelta(seconds=RETRY_S[min(attempts - 1, len(RETRY_S) - 1)]),
        }
        await self.col.update_one({"_id": doc_id}, {"$set": update})
        return update["status"]

    async def list(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        q = {"status": status} if status else {}
        return [d async for d in self.col.find(q).sort("updated_at", -1).limit(limit)]

    async def retry_dead(self) -> int:
        res = await self.col.update_many(
            {"status": "dead"},
            {"$set": {"status": "pending", "attempts": 0, "next_retry_at": utcnow(), "updated_at": utcnow()}},
        )
        return res.modified_count


class OutboxService:
    def __init__(
        self,
        *,
        repo: OutboxRepo,
        client: DingTalkClient | None,
        reports: ReportsRepo,
        users: UsersRepo,
        settings,
    ):
        self.repo = repo
        self.client = client
        self.reports = reports
        self.users = users
        self.settings = settings
        self._templates: dict[str, dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        return self.client is not None

    # --- enqueue -------------------------------------------------------------------------

    async def enqueue_report_sync(self, report: dict[str, Any]) -> bool:
        if not (self.enabled and self.settings.dingtalk_log_sync):
            return False
        user = await self.users.get(report["user_id"])
        if user is None or not user.dingtalk_id:
            return False
        queued = await self.repo.enqueue(
            "dingtalk.log", {"report_id": report["id"]}, dedupe_key=f"log:{report['id']}"
        )
        if queued:
            await self.reports.set_sync(report["id"], status="pending")
        return queued

    async def enqueue_message(
        self, user_id: str, title: str, text: str, link: str | None, dedupe_key: str | None
    ) -> bool:
        if not self.enabled:
            return False
        user = await self.users.get(user_id)
        if user is None or not user.dingtalk_id:
            return False
        return await self.repo.enqueue(
            "dingtalk.message",
            {"user_id": user_id, "title": title, "text": text, "link": link},
            dedupe_key=f"msg:{dedupe_key}" if dedupe_key else None,
        )

    # --- processing ------------------------------------------------------------------------

    async def process_due(self, max_items: int = 20) -> list[tuple[str, str]]:
        handled = []
        for _ in range(max_items):
            doc = await self.repo.claim()
            if doc is None:
                break
            try:
                await self._handle(doc)
                status = await self.repo.finish(doc["_id"])
            except PermanentError as e:
                status = await self.repo.finish(doc["_id"], error=str(e), permanent=True)
            except Exception as e:  # every other failure is recorded and retried
                log.warning("outbox delivery failed", extra={"type": doc["type"], "error": str(e)})
                status = await self.repo.finish(doc["_id"], error=f"{type(e).__name__}: {e}")
            if doc["type"] == "dingtalk.log" and status in ("pending", "dead"):
                await self.reports.set_sync(
                    doc["payload"]["report_id"],
                    status="failed" if status == "dead" else "pending",
                    last_error=await self._last_error(doc["_id"]),
                )
            handled.append((doc["type"], status))
        return handled

    async def _last_error(self, doc_id: Any) -> str | None:
        d = await self.repo.col.find_one({"_id": doc_id}, {"last_error": 1})
        return (d or {}).get("last_error")

    async def _handle(self, doc: dict[str, Any]) -> None:
        if self.client is None:
            raise PermanentError("DingTalk is not configured")
        if doc["type"] == "dingtalk.log":
            await self._sync_report(doc["payload"]["report_id"])
        elif doc["type"] == "dingtalk.message":
            await self._send_message(doc["payload"])
        else:
            raise PermanentError(f"unknown outbox type {doc['type']}")

    async def _template(self, staff_id: str) -> dict[str, Any]:
        name = self.settings.dingtalk_report_template
        if name not in self._templates:
            tpl = await self.client.get_template(name, staff_id)  # type: ignore[union-attr]
            if not tpl.get("id"):
                raise PermanentError(f"DingTalk log template '{name}' not found")
            self._templates[name] = tpl
        return self._templates[name]

    async def _sync_report(self, report_id: str) -> None:
        report = await self.reports.get(report_id)
        if report is None:
            raise PermanentError("report no longer exists")
        if (report.get("sync") or {}).get("status") == "synced":
            return
        user = await self.users.get(report["user_id"])
        if user is None or not user.dingtalk_id:
            raise PermanentError("the author has no DingTalk account")
        st = self.settings
        state = template_state(st.resolve(st.dingtalk_template_path), st.resolve(st.dingtalk_mapping_path))
        if not state.usable:
            # retried with backoff; once remapped, `dl dingtalk outbox --retry-dead` resends
            raise RuntimeError(
                "the DingTalk log template changed (or has no confirmed mapping): run `dl template map`"
            )
        tpl = await self._template(user.dingtalk_id)
        first_field = next(
            (
                f.get("field_name")
                for f in sorted(tpl.get("fields") or [], key=lambda f: int(f.get("sort", 0)))
            ),
            None,
        )
        contents = render_contents(
            report.get("output") or {},
            None if state.empty else state.mapping,
            lang=report.get("lang", "zh"),
            default_field=first_field,
        )
        manager = await self.users.get(user.manager_id) if user.manager_id else None
        log_id = await self.client.create_report(  # type: ignore[union-attr]
            userid=user.dingtalk_id,
            template_id=str(tpl["id"]),
            contents=contents,
            to_userids=[manager.dingtalk_id] if manager and manager.dingtalk_id else None,
        )
        await self.reports.set_sync(report_id, status="synced", dingtalk_log_id=log_id)
        log.info("report synced to DingTalk", extra={"report": report_id, "log": log_id})

    async def _send_message(self, p: dict[str, Any]) -> None:
        user = await self.users.get(p["user_id"])
        if user is None or not user.dingtalk_id:
            raise PermanentError("the recipient has no DingTalk account")
        text = f"#### {p['title']}\n\n{p.get('text') or ''}"
        if p.get("link") and self.settings.public_url:
            text += f"\n\n[打开 Dayline]({self.settings.public_url.rstrip('/')}{p['link']})"
        await self.client.send_markdown([user.dingtalk_id], p["title"], text)  # type: ignore[union-attr]
