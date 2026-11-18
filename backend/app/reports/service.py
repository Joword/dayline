"""Report workflows shared by the CLI, the API and the worker.

The roll-up flow (plan §4.3):

1. ``draft_rollup``  - runs team-rollup as the manager; the result plus the manager's
   own items becomes a *draft* manager report. Nothing is sent.
2. ``confirm_rollup`` - the manager picks which escalation candidates go up and which
   items held by them are forwarded further (M5 escalation chain); the confirmed report
   is what the manager's manager sees. The model's draft stays in ``ai_draft`` so it can
   be compared with the final version.

When a member submits a newer version after their manager's roll-up cited an older one,
the manager is notified (plan §4.3, snapshots).
"""

from __future__ import annotations

from typing import Any

from app.db.models import EscalationOrigin, ReviewDoc
from app.db.repositories.misc import ReviewsRepo
from app.db.repositories.reports import ReportsRepo
from app.escalations.service import EscalationService, entry
from app.notify.service import Notifier
from app.relay.service import Relay
from app.render.markdown import render_daily_report
from app.skills.runner import SkillRunner, SkillRunResult


class ReportError(Exception):
    pass


class ReportForbidden(ReportError):
    pass


class ReportNotFound(ReportError):
    pass


class ReportService:
    def __init__(
        self,
        *,
        runner: SkillRunner,
        relay: Relay,
        reports: ReportsRepo,
        reviews: ReviewsRepo,
        escalations: EscalationService | None = None,
        notifier: Notifier | None = None,
        outbox=None,  # app.dingtalk.outbox.OutboxService (M6)
    ):
        self.runner = runner
        self.relay = relay
        self.reports = reports
        self.reviews = reviews
        self.escalations = escalations
        self.notifier = notifier or Notifier(None)
        self.outbox = outbox

    async def _own(self, actor_id: str, report_id: str) -> dict[str, Any]:
        doc = await self.reports.get(report_id)
        if doc is None:
            raise ReportNotFound("report not found")
        if doc["user_id"] != actor_id:
            raise ReportForbidden("only the author can change a report")
        return doc

    # --- individual reports ---------------------------------------------------------

    async def save_generated(
        self, user_id: str, result: SkillRunResult, raw_input: dict[str, Any]
    ) -> dict[str, Any]:
        if result.status != "ok":
            raise ReportError("cannot save an output that failed validation")
        return await self.reports.save_draft(
            user_id=user_id,
            date=result.output["date"],
            lang=result.lang,
            raw_input=raw_input,
            output=result.output,
            rendered_md=result.markdown,
            skill_name=result.skill,
            skill_version=result.version,
            skill_run_id=result.run_id,
        )

    async def confirm(self, actor_id: str, report_id: str) -> dict[str, Any]:
        doc = await self._own(actor_id, report_id)
        if doc["status"] != "draft":
            raise ReportError(f"report is already {doc['status']}")
        out = doc.get("output") or {}
        rendered = render_daily_report(out, doc.get("lang", "zh")) if out.get("team_summary") else None
        confirmed = await self.reports.confirm(report_id, rendered_md=rendered)
        if confirmed is None:
            raise ReportError("report could not be confirmed")
        await self._alert_changed_snapshot(confirmed)
        await self._sync(confirmed)
        return confirmed

    async def _sync(self, confirmed: dict[str, Any]) -> None:
        """One-way sync of the confirmed report to DingTalk logs, via the outbox."""
        if self.outbox is not None:
            await self.outbox.enqueue_report_sync(confirmed)

    async def _alert_changed_snapshot(self, confirmed: dict[str, Any]) -> None:
        """If the author's manager rolled up an older version of this day, tell them."""
        author = await self.relay.users.get(confirmed["user_id"])
        if author is None or not author.manager_id:
            return
        mgr_report = await self.reports.latest(author.manager_id, confirmed["date"])
        ts = (mgr_report or {}).get("team_summary") or {}
        cited = next((s for s in ts.get("source_reports") or [] if s.get("user_id") == author.id), None)
        if cited and cited.get("version", 0) < confirmed["version"]:
            state = "已发送的" if mgr_report and mgr_report["status"] != "draft" else "草稿中的"
            await self.notifier.notify(
                [author.manager_id],
                "report.changed",
                f"{author.name} 更新了 {confirmed['date']} 的日报(v{cited['version']} → v{confirmed['version']})",
                body=f"你{state}团队汇总引用的是旧版本,可以重新生成汇总。",
                link=f"/team?date={confirmed['date']}",
                dedupe_key=f"report.changed:{author.id}:{confirmed['date']}:{confirmed['version']}",
            )

    # --- manager: review a direct report ---------------------------------------------

    async def review_member(
        self,
        manager_id: str,
        member_id: str,
        date: str | None,
        *,
        tone: str = "encouraging",
        lang: str | None = None,
    ) -> tuple[SkillRunResult, str | None]:
        result = await self.runner.run(
            "review-manager", {"member": member_id, "date": date, "tone": tone}, user_id=manager_id, lang=lang
        )
        if result.status != "ok":
            return result, None
        meta = result.context.get("member_report", {})
        out = result.output
        review_id = await self.reviews.insert(
            ReviewDoc(
                report_id=meta.get("report_id"),
                report_version=meta.get("report_version"),
                subject_id=member_id,
                reviewer_id=manager_id,
                skill=result.skill,
                rating=out.get("rating"),
                issues=out.get("issues", []),
                questions=out.get("questions", []),
                feedback_draft=out.get("feedback_draft", ""),
                output=out,
                skill_run_id=result.run_id,
            )
        )
        return result, review_id

    # --- manager: team roll-up ----------------------------------------------------------

    async def draft_rollup(
        self, manager_id: str, date: str | None, *, lang: str | None = None
    ) -> tuple[SkillRunResult, dict[str, Any] | None, list[str]]:
        result = await self.runner.run("team-rollup", {"date": date}, user_id=manager_id, lang=lang)
        if result.status != "ok":
            return result, None, []
        day = date or self.runner.today().isoformat()
        warnings: list[str] = []
        own = await self.reports.latest(manager_id, day)
        if own is None:
            warnings.append("no personal report for this day yet; the manager report has team content only")
        team_summary = dict(result.output)
        if self.escalations is not None:
            names = team_summary.get("member_names") or {}
            team_summary["pending_escalations"] = [
                entry(e, names) for e in await self.escalations.pending_for(manager_id)
            ]
            team_summary["raised_escalations"] = [
                entry(e, names)
                for e in await self.escalations.raised_on(manager_id, day)
                if e.current_holder_id != manager_id
            ]
        output = {
            "kind": "manager",
            "date": day,
            "lang": result.lang,
            "own_items": (own or {}).get("own_items", []),
            "plan_check": ((own or {}).get("output") or {}).get("plan_check", []),
            "questions": [],
            "team_summary": team_summary,
        }
        doc = await self.reports.save_draft(
            user_id=manager_id,
            date=day,
            lang=result.lang,
            raw_input={"rollup_run_id": result.run_id},
            output=output,
            rendered_md=render_daily_report(output, result.lang),
            skill_name=result.skill,
            skill_version=result.version,
            skill_run_id=result.run_id,
            kind="manager",
        )
        return result, doc, warnings

    async def confirm_rollup(
        self,
        manager_id: str,
        report_id: str,
        *,
        escalate: list[int] | None = None,
        forward: list[str] | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        """`escalate`: 1-based indexes of the AI's escalation candidates to raise and send up.
        `forward`: ids of escalations held by the manager to pass one level further up."""
        doc = await self._own(manager_id, report_id)
        ts = doc.get("team_summary")
        if not ts:
            raise ReportError("not a manager report")
        if doc["status"] != "draft":
            raise ReportError(f"report is already {doc['status']}")
        newest = await self.reports.latest(manager_id, doc["date"])
        if newest and newest["id"] != report_id:
            raise ReportError(f"v{doc['version']} is outdated; confirm v{newest['version']}")
        candidates = ts.get("escalation_candidates") or []
        picked = []
        for i in escalate or []:
            if not 1 <= i <= len(candidates):
                raise ReportError(f"no escalation candidate #{i}")
            picked.append(candidates[i - 1])
        pending_ids = {e["escalation_id"] for e in ts.get("pending_escalations") or []}
        for esc_id in forward or []:
            if esc_id not in pending_ids:
                raise ReportError(f"escalation {esc_id} is not waiting for you")
        if forward and self.escalations is None:
            raise ReportError("escalations are not available")

        entries: list[dict[str, Any]] = []
        names = ts.get("member_names") or {}
        if self.escalations is not None:
            sources = {s["user_id"]: s for s in ts.get("source_reports") or []}
            for c in picked:
                member = next((m for m in c.get("members") or [] if m in sources), None)
                src = sources.get(member or "")
                esc = await self.escalations.raise_item(
                    manager_id,
                    text=c["text"],
                    origin=EscalationOrigin(
                        user_id=member or manager_id,
                        date=doc["date"],
                        report_id=src["report_id"] if src else None,
                        report_version=src["version"] if src else None,
                    ),
                    need_from=c.get("need_from"),
                    note=c.get("reason"),
                )
                entries.append(entry(esc, names) | {"reason": c.get("reason")})
            for esc_id in forward or []:
                entries.append(entry(await self.escalations.escalate(manager_id, esc_id), names))
            # items raised earlier that day (e.g. from a member review) go up with this report
            seen = {e["escalation_id"] for e in entries}
            for esc in await self.escalations.raised_on(manager_id, doc["date"]):
                if esc.id not in seen and esc.current_holder_id != manager_id:
                    entries.append(entry(esc, names))
        else:
            entries = picked

        drop = {"escalation_candidates", "pending_escalations", "raised_escalations"}
        final_ts = {k: v for k, v in ts.items() if k not in drop} | {"escalations": entries}
        output = {**(doc.get("output") or {}), "team_summary": final_ts}
        lang = doc.get("lang", "zh")
        confirmed = await self.reports.confirm(
            report_id, output=output, rendered_md=render_daily_report(output, lang)
        )
        if confirmed is None:
            raise ReportError("report could not be confirmed")
        await self._sync(confirmed)
        return confirmed, await self.stale_sources(final_ts, doc["date"])

    async def stale_sources(self, team_summary: dict[str, Any], date: str) -> list[str]:
        """Members who submitted a newer version after the roll-up was drafted."""
        stale = []
        for src in team_summary.get("source_reports") or []:
            latest = await self.reports.latest_submitted(src["user_id"], date)
            if latest and latest["version"] != src["version"]:
                stale.append(src["user_id"])
        return stale
