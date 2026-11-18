"""Escalation chain (plan §4.4).

A blocker a manager marks "needs my manager" becomes an escalation:

    open         held at the raising level and tracked there (nobody above, or "track only")
    escalated    passed to the holder's manager, waiting for them
    in_progress  the current holder took it on
    resolved     settled at some level, with a resolution note

At every level the holder can accept, resolve locally or forward one level up. Every
step is appended to ``chain`` (who, what, from whom to whom, note). Transitions are
atomic compare-and-set on (holder, status), so two people acting at once cannot both win.

Visibility: participants only - the people in the chain plus the member whose blocker it
was. Escalated items are things a manager deliberately passed up, like the escalations in
their team summary; the skip-level manager still cannot read the member's report itself.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.db.models import EscalationDoc, EscalationEvent, EscalationOrigin, UserDoc
from app.db.repositories.escalations import EscalationsRepo
from app.db.repositories.users import UsersRepo
from app.notify.service import Notifier
from app.relay.service import Relay

log = logging.getLogger("dayline.escalations")

OPEN_STATES = ["open", "escalated", "in_progress"]


class EscalationError(Exception):
    pass


class EscalationForbidden(EscalationError):
    pass


class EscalationNotFound(EscalationError):
    pass


class EscalationService:
    def __init__(self, *, repo: EscalationsRepo, users: UsersRepo, relay: Relay, notifier: Notifier):
        self.repo = repo
        self.users = users
        self.relay = relay
        self.notifier = notifier

    async def _user(self, user_id: str) -> UserDoc:
        u = await self.users.get(user_id)
        if u is None:
            raise EscalationForbidden(f"unknown or inactive user: {user_id}")
        return u

    def _link(self, esc_id: str) -> str:
        return f"/escalations/{esc_id}"

    # --- raising ------------------------------------------------------------------------

    async def raise_item(
        self,
        actor_id: str,
        *,
        text: str,
        origin: EscalationOrigin,
        need_from: str | None = None,
        severity: str | None = None,
        note: str | None = None,
        escalate: bool = True,
    ) -> EscalationDoc:
        actor = await self._user(actor_id)
        if origin.user_id != actor_id:
            member = await self.users.get(origin.user_id)
            if member is None or member.manager_id != actor_id:
                raise EscalationForbidden("only the direct manager can raise a member's blocker")
        if escalate and actor.manager_id:
            holder, status = actor.manager_id, "escalated"
        else:
            holder, status = actor_id, "open"
        chain = [
            EscalationEvent(
                actor_id=actor_id,
                action="created",
                to_holder=actor_id,
                note=None if status == "escalated" else note,
            )
        ]
        if status == "escalated":
            chain.append(
                EscalationEvent(
                    actor_id=actor_id, action="escalated", from_holder=actor_id, to_holder=holder, note=note
                )
            )
        doc = await self.repo.insert(
            EscalationDoc(
                id=uuid.uuid4().hex[:12],
                text=text,
                need_from=need_from,
                severity=severity,  # type: ignore[arg-type]
                origin=origin,
                created_by=actor_id,
                current_holder_id=holder,
                status=status,  # type: ignore[arg-type]
                participants=list(dict.fromkeys([actor_id, origin.user_id, holder])),
                chain=chain,
            )
        )
        if status == "escalated":
            await self.notifier.notify(
                [holder],
                "escalation.escalated",
                f"{actor.name} 上报了一个需要你介入的事项",
                body=text,
                link=self._link(doc.id),
                dedupe_key=f"esc:{doc.id}:escalated:{holder}",
            )
        log.info(
            "escalation raised", extra={"id": doc.id, "by": actor_id, "holder": holder, "status": status}
        )
        return doc

    async def raise_from_report(
        self,
        actor_id: str,
        member_id: str,
        date: str,
        item_index: int,
        *,
        escalate: bool = True,
        note: str | None = None,
    ) -> EscalationDoc:
        """Raise blocker #item_index (0-based) of a report the actor may read: a direct
        report's submitted report, or the actor's own."""
        viewer = await self.relay.viewer(actor_id)
        v = await self.relay.get_report(viewer, member_id, date)
        if v.access != "full":
            raise EscalationForbidden("only the direct manager can raise a member's blocker")
        if not 0 <= item_index < len(v.own_items):
            raise EscalationError(f"the report has no item #{item_index}")
        item = v.own_items[item_index]
        if item.get("kind") != "blocker":
            raise EscalationError("only blockers can be escalated")
        dup = await self.repo.find(
            {
                "origin.user_id": member_id,
                "origin.date": date,
                "text": item.get("text"),
                "status": {"$in": OPEN_STATES},
            },
            limit=1,
        )
        if dup:
            raise EscalationError(f"already raised as {dup[0].id} ({dup[0].status})")
        return await self.raise_item(
            actor_id,
            text=item.get("text", ""),
            origin=EscalationOrigin(user_id=member_id, date=date, report_id=v.id, report_version=v.version),
            need_from=item.get("need_from"),
            severity=item.get("severity"),
            note=note,
            escalate=escalate,
        )

    # --- reading ------------------------------------------------------------------------

    async def get(self, actor_id: str, esc_id: str) -> EscalationDoc:
        doc = await self.repo.get(esc_id)
        if doc is None:
            raise EscalationNotFound("escalation not found")
        if actor_id not in doc.participants:
            raise EscalationForbidden("you are not part of this escalation")
        return doc

    async def find_for(
        self, actor_id: str, scope: str = "held", *, include_resolved: bool = False
    ) -> list[EscalationDoc]:
        q: dict[str, Any] = {
            "held": {"current_holder_id": actor_id},
            "raised": {"created_by": actor_id},
            "involved": {"participants": actor_id},
        }.get(scope) or {"participants": actor_id}
        if not include_resolved:
            q["status"] = {"$in": OPEN_STATES}
        return await self.repo.find(q)

    # --- transitions --------------------------------------------------------------------

    async def _apply(
        self,
        actor_id: str,
        esc_id: str,
        *,
        allowed: list[str],
        event: EscalationEvent,
        set_: dict[str, Any],
        add: str | None = None,
    ) -> EscalationDoc:
        doc = await self.get(actor_id, esc_id)
        if doc.current_holder_id != actor_id:
            raise EscalationForbidden("only the current holder can do this")
        if doc.status not in allowed:
            raise EscalationError(f"not possible while {doc.status}")
        out = await self.repo.transition(
            esc_id, expect_holder=actor_id, expect_status=allowed, event=event, set_=set_, add_participant=add
        )
        if out is None:
            raise EscalationError("the escalation changed meanwhile; reload and try again")
        return out

    async def accept(self, actor_id: str, esc_id: str, note: str | None = None) -> EscalationDoc:
        doc = await self._apply(
            actor_id,
            esc_id,
            allowed=["open", "escalated"],
            event=EscalationEvent(actor_id=actor_id, action="accepted", note=note),
            set_={"status": "in_progress"},
        )
        actor = await self._user(actor_id)
        await self.notifier.notify(
            [p for p in [doc.created_by] if p != actor_id],
            "escalation.accepted",
            f"{actor.name} 已接手:{doc.text[:40]}",
            body=note or "",
            link=self._link(doc.id),
        )
        return doc

    async def escalate(self, actor_id: str, esc_id: str, note: str | None = None) -> EscalationDoc:
        actor = await self._user(actor_id)
        if not actor.manager_id:
            raise EscalationError("you have no manager to escalate to; resolve it or keep it in progress")
        doc = await self._apply(
            actor_id,
            esc_id,
            allowed=OPEN_STATES,
            event=EscalationEvent(
                actor_id=actor_id,
                action="escalated",
                from_holder=actor_id,
                to_holder=actor.manager_id,
                note=note,
            ),
            set_={"status": "escalated", "current_holder_id": actor.manager_id},
            add=actor.manager_id,
        )
        await self.notifier.notify(
            [actor.manager_id],
            "escalation.escalated",
            f"{actor.name} 上报了一个需要你介入的事项",
            body=doc.text,
            link=self._link(doc.id),
            dedupe_key=f"esc:{doc.id}:escalated:{actor.manager_id}",
        )
        return doc

    async def resolve(self, actor_id: str, esc_id: str, note: str) -> EscalationDoc:
        if not note.strip():
            raise EscalationError("a resolution note is required")
        doc = await self._apply(
            actor_id,
            esc_id,
            allowed=OPEN_STATES,
            event=EscalationEvent(actor_id=actor_id, action="resolved", note=note),
            set_={"status": "resolved", "resolution": note},
        )
        actor = await self._user(actor_id)
        await self.notifier.notify(
            [p for p in doc.participants if p != actor_id],
            "escalation.resolved",
            f"{actor.name} 已解决:{doc.text[:40]}",
            body=note,
            link=self._link(doc.id),
        )
        return doc

    async def comment(self, actor_id: str, esc_id: str, note: str) -> EscalationDoc:
        if not note.strip():
            raise EscalationError("comment is empty")
        doc = await self.get(actor_id, esc_id)
        out = await self.repo.comment(
            esc_id, EscalationEvent(actor_id=actor_id, action="commented", note=note)
        )
        assert out is not None
        if doc.current_holder_id != actor_id:
            actor = await self._user(actor_id)
            await self.notifier.notify(
                [doc.current_holder_id],
                "escalation.commented",
                f"{actor.name} 补充了信息:{doc.text[:40]}",
                body=note,
                link=self._link(doc.id),
            )
        return out

    # --- roll-up support ------------------------------------------------------------------

    async def pending_for(self, manager_id: str) -> list[EscalationDoc]:
        """Items waiting for this manager (passed up to them, or taken on by them)."""
        return await self.repo.find(
            {"current_holder_id": manager_id, "status": {"$in": ["escalated", "in_progress"]}}
        )

    async def raised_on(self, manager_id: str, date: str) -> list[EscalationDoc]:
        """Items this manager raised from that day's reports and that are still open upstream."""
        return await self.repo.find(
            {"created_by": manager_id, "origin.date": date, "status": {"$in": OPEN_STATES}}
        )


def entry(doc: EscalationDoc, names: dict[str, str] | None = None) -> dict[str, Any]:
    """Compact form used inside team summaries."""
    names = names or {}
    return {
        "escalation_id": doc.id,
        "text": doc.text,
        "need_from": doc.need_from,
        "status": doc.status,
        "holder": names.get(doc.current_holder_id, doc.current_holder_id),
        "origin": names.get(doc.origin.user_id, doc.origin.user_id),
    }
