"""Notifications: in-app, plus a DingTalk message through the outbox (M6) for users with a
DingTalk account.

``dedupe_key`` makes a notification idempotent per recipient (e.g. one "report missing"
reminder per person per day, even if a job is retried).
"""

from __future__ import annotations

import logging

from app.db.models import NotificationDoc
from app.db.repositories.escalations import NotificationsRepo

log = logging.getLogger("dayline.notify")


class Notifier:
    def __init__(self, repo: NotificationsRepo | None, outbox=None):
        self.repo = repo
        self.outbox = outbox  # app.dingtalk.outbox.OutboxService (optional)

    async def notify(
        self,
        user_ids: list[str],
        kind: str,
        title: str,
        *,
        body: str = "",
        link: str | None = None,
        dedupe_key: str | None = None,
    ) -> int:
        sent = 0
        for uid in dict.fromkeys(user_ids):
            doc = NotificationDoc(
                user_id=uid,
                kind=kind,
                title=title,
                body=body,
                link=link,
                dedupe_key=f"{dedupe_key}:{uid}" if dedupe_key else None,
            )
            if self.repo is None:
                log.info("notification (not stored)", extra={"user": uid, "kind": kind, "title": title})
                sent += 1
                continue
            if await self.repo.add(doc):
                sent += 1
                if self.outbox is not None:
                    try:
                        await self.outbox.enqueue_message(uid, title, body, link, doc.dedupe_key)
                    except Exception:
                        log.exception("could not queue DingTalk message", extra={"user": uid})
        return sent
