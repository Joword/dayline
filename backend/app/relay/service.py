"""Relay: the single gate for reading report and org data.

Skills (through context providers and tools), the API and the worker read data
only through this class, so the visibility rules live in one place
(``policy.py``). Every access to someone else's data is written to
``audit_logs`` - allowed or denied - with the client id, ready for the
relay-mcp extension (E1).
"""

from __future__ import annotations

import logging
from typing import Any

from app.db.models import SUBMITTED, AuditDoc, Consent, UserDoc
from app.db.repositories.misc import AuditRepo
from app.db.repositories.reports import ReportsRepo
from app.db.repositories.users import UsersRepo
from app.relay.policy import Access, Decision, Relation, Resource, Viewer, decide, relation_of
from app.relay.views import ReportView, TeamMember, TeamSnapshot, UserBrief
from app.render.markdown import render_daily_report

log = logging.getLogger("dayline.relay")


class RelayError(Exception):
    pass


class RelayForbidden(RelayError):
    pass


class RelayNotFound(RelayError):
    pass


def brief(u: UserDoc) -> UserBrief:
    return UserBrief(id=u.id, name=u.name, org_unit=u.org_unit)


def to_view(doc: dict[str, Any], user: UserDoc, access: Access, *, owner: bool) -> ReportView:
    output = doc.get("output") or {}
    items = doc.get("own_items") or []
    view = ReportView(
        id=doc["id"],
        user=brief(user),
        date=doc["date"],
        kind=doc.get("kind", "individual"),
        version=doc["version"],
        status=doc["status"],
        lang=doc.get("lang", "zh"),
        access=access,
        own_items=items,
        plan_check=output.get("plan_check") or [],
        team_summary=doc.get("team_summary"),
        rendered_md=doc.get("rendered_md", ""),
        raw_input=doc.get("raw_input") if owner else None,
        confirmed_at=doc.get("confirmed_at"),
        sync_status=(doc.get("sync") or {}).get("status", "none"),
    )
    if access == "dependencies":
        deps = [i for i in items if i.get("cross_team")]
        view.own_items = deps
        view.plan_check = []
        view.team_summary = None
        view.rendered_md = render_daily_report({"date": doc["date"], "own_items": deps}, view.lang)
    return view


class Relay:
    def __init__(
        self, users: UsersRepo, reports: ReportsRepo, audit: AuditRepo | None = None, activity: Any = None
    ):
        self.users = users
        self.reports = reports
        self.audit = audit
        self.activity_service = activity  # app.integrations.service.ActivityService (M3)

    # --- identity -----------------------------------------------------------------

    async def viewer(
        self, user_id: str, *, client_id: str = "dayline", scopes: frozenset[str] | None = None
    ) -> Viewer:
        if await self.users.get(user_id) is None:
            raise RelayNotFound(f"unknown or inactive user: {user_id}")
        return Viewer(user_id=user_id, client_id=client_id, scopes=scopes or frozenset({"*"}))

    async def user(self, user_id: str) -> UserDoc:
        u = await self.users.get(user_id)
        if u is None:
            raise RelayNotFound(f"unknown or inactive user: {user_id}")
        return u

    async def is_manager(self, user_id: str) -> bool:
        return await self.users.has_reports(user_id)

    async def direct_reports(self, viewer: Viewer) -> list[UserBrief]:
        return [brief(u) for u in await self.users.direct_reports(viewer.user_id)]

    # --- decisions ----------------------------------------------------------------

    async def _relation(self, me: UserDoc, target: UserDoc) -> Relation:
        if me.id == target.id or target.manager_id == me.id:
            return relation_of(me, target, viewer_manages=False, target_manages=False)
        return relation_of(
            me,
            target,
            viewer_manages=await self.users.has_reports(me.id),
            target_manages=await self.users.has_reports(target.id),
        )

    async def check(
        self,
        viewer: Viewer,
        target_id: str,
        resource: Resource,
        *,
        action: str,
        activity_scope: str | None = None,
    ) -> tuple[Decision, UserDoc]:
        me = await self.user(viewer.user_id)
        target = await self.users.get(target_id)
        if target is None:
            await self._audit(
                viewer, action, f"user:{target_id}", Decision(False, "none", "other", "unknown user")
            )
            raise RelayNotFound(f"unknown or inactive user: {target_id}")
        rel = await self._relation(me, target)
        d = decide(viewer, rel, resource, target=target, activity_scope=activity_scope)
        if rel != "self":
            await self._audit(viewer, action, f"user:{target_id}", d)
        if not d.allowed:
            raise RelayForbidden(d.reason)
        return d, target

    async def _audit(self, viewer: Viewer, action: str, target: str, d: Decision) -> None:
        if self.audit is None:
            return
        try:
            await self.audit.write(
                AuditDoc(
                    actor_id=viewer.user_id,
                    client_id=viewer.client_id,
                    action=action,
                    target=target,
                    allowed=d.allowed,
                    access=d.access,
                    reason=d.reason,
                )
            )
        except Exception:
            log.exception("audit write failed")

    # --- reports ------------------------------------------------------------------

    async def get_report(self, viewer: Viewer, user_id: str, date: str) -> ReportView:
        d, target = await self.check(viewer, user_id, "report", action="report.read")
        owner = d.relation == "self"
        doc = await (
            self.reports.latest(user_id, date) if owner else self.reports.latest_submitted(user_id, date)
        )
        if doc is None:
            raise RelayNotFound(f"no report for {user_id} on {date}")
        return to_view(doc, target, d.access, owner=owner)

    async def get_report_by_id(self, viewer: Viewer, report_id: str) -> ReportView:
        doc = await self.reports.get(report_id)
        if doc is None:
            raise RelayNotFound("report not found")
        d, target = await self.check(viewer, doc["user_id"], "report", action="report.read")
        owner = d.relation == "self"
        if not owner and doc["status"] not in SUBMITTED:
            raise RelayNotFound("report not found")
        return to_view(doc, target, d.access, owner=owner)

    async def history(
        self,
        viewer: Viewer,
        user_id: str,
        *,
        before: str | None = None,
        since: str | None = None,
        limit: int = 5,
    ) -> list[ReportView]:
        d, target = await self.check(viewer, user_id, "report", action="report.history")
        docs = await self.reports.list_recent(user_id, before=before, since=since, limit=limit)
        return [to_view(x, target, d.access, owner=d.relation == "self") for x in docs]

    async def team_snapshot(self, viewer: Viewer, date: str) -> TeamSnapshot:
        """All direct reports and their latest submitted report for `date`, fetched by code
        from the org tree - the model never chooses whose reports it sees."""
        me = await self.user(viewer.user_id)
        members = await self.users.direct_reports(me.id)
        if not members:
            raise RelayForbidden("you have no direct reports")
        if not viewer.has_scope("read:reports"):
            raise RelayForbidden(f"client '{viewer.client_id}' lacks scope read:reports")
        docs = await self.reports.latest_submitted_many([m.id for m in members], date)
        await self._audit(
            viewer,
            "team.read",
            f"team:{me.id}/{date}",
            Decision(True, "full", "direct_manager", "direct reports"),
        )
        return TeamSnapshot(
            manager=brief(me),
            date=date,
            members=[
                TeamMember(
                    user=brief(m),
                    report=to_view(docs[m.id], m, "full", owner=False) if m.id in docs else None,
                )
                for m in members
            ],
        )

    # --- activity (git / calendar) ------------------------------------------------------

    async def activity(
        self, viewer: Viewer, user_id: str, date: str, *, refresh: bool = False
    ) -> dict[str, Any]:
        """Git and calendar activity of `user_id` on `date`, per source as the policy allows:
        the person themselves, or their direct manager with consent for that source.
        Sources the viewer may not see are not even fetched."""
        if self.activity_service is None:
            raise RelayNotFound("activity sources are not configured")
        allowed: list[str] = []
        unavailable: dict[str, str] = {}
        target: UserDoc | None = None
        for source in ("git", "calendar"):
            try:
                _, target = await self.check(
                    viewer, user_id, "activity", action="activity.read", activity_scope=source
                )
                allowed.append(source)
            except RelayForbidden as e:
                unavailable[source] = str(e)
        out: dict[str, Any] = {
            "date": date,
            "git": None,
            "calendar": None,
            "unavailable": unavailable,
            "errors": {},
        }
        if not allowed or target is None:
            return out
        data = await self.activity_service.collect(
            target, date, refresh=refresh and viewer.user_id == user_id, sources=tuple(allowed)
        )
        for source in allowed:
            out[source] = data.get(source)
        out["errors"] = {k: v for k, v in data.get("errors", {}).items() if k in allowed}
        return out

    # --- consent ------------------------------------------------------------------

    async def grant_consent(self, viewer: Viewer, scope: str) -> Consent:
        me = await self.user(viewer.user_id)
        if not me.manager_id:
            raise RelayForbidden("you have no manager to share data with")
        c = Consent(scope=scope, grantee_id=me.manager_id)  # type: ignore[arg-type]
        await self.users.set_consent(me.id, c)
        await self._audit(
            viewer,
            "consent.grant",
            f"user:{me.id}/{scope}->{me.manager_id}",
            Decision(True, "full", "self", "granted"),
        )
        return c

    async def revoke_consent(self, viewer: Viewer, scope: str) -> bool:
        ok = await self.users.remove_consent(viewer.user_id, scope)
        await self._audit(
            viewer,
            "consent.revoke",
            f"user:{viewer.user_id}/{scope}",
            Decision(True, "full", "self", "revoked"),
        )
        return ok

    async def consents(self, viewer: Viewer) -> list[Consent]:
        me = await self.user(viewer.user_id)
        return [c for c in me.consents if c.grantee_id == me.manager_id]
