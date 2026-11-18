"""Built-in context providers (manager review and team roll-up).

Each provider is two halves:

* fetch   - read through Relay as the requesting user and produce ``data``;
* replay  - a pure function ``data -> ContextResult`` (overlay, quote source, meta).

Because the second half needs nothing but ``data``, a skill example can carry the
context it was written against (``"context": {...}`` in ``examples/*.json``): few-shot
prompts show it, evals replay it without a database, and the reference outputs are
checked against the same rules a real run applies.
"""

from __future__ import annotations

from typing import Any

from app.relay.service import RelayNotFound
from app.relay.views import ReportView
from app.rules.trends import Signal, compute_trends, on_track
from app.skills.context import ContextRequest, ContextResult, context_provider

HISTORY_REPORTS = 5
ITEM_FIELDS = ("kind", "text", "progress", "eta", "need_from", "severity", "priority", "cross_team")


def _report_dict(v: ReportView) -> dict[str, Any]:
    return {"date": v.date, "own_items": v.own_items}


def _items_for_prompt(v: ReportView) -> list[dict[str, Any]]:
    return [{k: i[k] for k in ITEM_FIELDS if i.get(k) not in (None, "", False)} for i in v.own_items]


async def _signals(
    req: ContextRequest, member_id: str, current: ReportView | None, date: str
) -> tuple[list[Signal], list[ReportView]]:
    days = int(req.params.get("days", HISTORY_REPORTS))
    history = await req.relay.history(req.viewer, member_id, before=date, limit=days)
    signals = compute_trends(
        _report_dict(current) if current else None, [_report_dict(h) for h in history], today=date
    )
    return signals, history


# --- replay halves (pure) ---------------------------------------------------------------


def replay_member_report(data: dict[str, Any]) -> ContextResult:
    texts = [i.get("text", "") for i in data.get("items", [])]
    texts += [p.get("plan_text", "") for p in data.get("plan_check", [])]
    return ContextResult(data=data, quote_source="\n".join(texts))


def replay_member_trends(data: dict[str, Any]) -> ContextResult:
    return ContextResult(data=data, meta={"signal_texts": [s["text"] for s in data.get("signals", [])]})


def replay_team_snapshot(data: dict[str, Any]) -> ContextResult:
    members, missing = data.get("members", []), data.get("missing", [])
    return ContextResult(
        data=data,
        overlay={
            "source_reports": [],  # filled in by the fetch half (report ids are not in `data`)
            "stats": {
                "members": len(members) + len(missing),
                "submitted": len(members),
                "on_track": sum(1 for m in members if m.get("on_track")),
            },
            "missing": missing,
            "member_names": {m["id"]: m["name"] for m in [*members, *missing]},
        },
        meta={"member_ids": [m["id"] for m in members], "missing_ids": [u["id"] for u in missing]},
    )


# --- providers ------------------------------------------------------------------------


@context_provider(
    "member_report",
    permission="read:team_reports",
    description="A direct report's report for the date",
    replay=replay_member_report,
)
async def member_report(req: ContextRequest) -> ContextResult:
    member, date = req.inputs["member"], req.inputs.get("date") or req.run_date
    v: ReportView = await req.relay.get_report(req.viewer, member, date)
    res = replay_member_report(
        {
            "member": v.user.model_dump(),
            "date": v.date,
            "version": v.version,
            "items": _items_for_prompt(v),
            "plan_check": v.plan_check,
        }
    )
    res.quote_source = "\n".join([v.rendered_md, res.quote_source or ""])
    res.meta = {"report_id": v.id, "report_version": v.version, "subject_id": v.user.id}
    return res


@context_provider(
    "member_trends",
    permission="read:team_reports",
    description="Cross-day signals for a direct report",
    replay=replay_member_trends,
)
async def member_trends(req: ContextRequest) -> ContextResult:
    member, date = req.inputs["member"], req.inputs.get("date") or req.run_date
    try:
        current = await req.relay.get_report(req.viewer, member, date)
    except RelayNotFound:
        current = None
    signals, history = await _signals(req, member, current, date)
    return replay_member_trends(
        {
            "signals": [s.to_dict() for s in signals],
            "earlier_reports": [{"date": h.date, "items": _items_for_prompt(h)} for h in history],
        }
    )


@context_provider(
    "team_snapshot",
    permission="read:team_reports",
    description="All direct reports' submitted reports for the date",
    replay=replay_team_snapshot,
)
async def team_snapshot(req: ContextRequest) -> ContextResult:
    date = req.inputs.get("date") or req.run_date
    snap = await req.relay.team_snapshot(req.viewer, date)
    members: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for m in snap.members:
        if m.report is None:
            continue  # missing members are listed, never written up from other data
        signals, _ = await _signals(req, m.user.id, m.report, date)
        sources.append({"report_id": m.report.id, "version": m.report.version, "user_id": m.user.id})
        members.append(
            {
                "id": m.user.id,
                "name": m.user.name,
                "on_track": on_track(_report_dict(m.report), signals),
                "items": _items_for_prompt(m.report),
                "signals": [s.to_dict() for s in signals],
            }
        )
    missing = [{"id": u.id, "name": u.name} for u in snap.missing]
    res = replay_team_snapshot({"date": date, "members": members, "missing": missing})
    assert res.overlay is not None
    res.overlay["source_reports"] = sources
    return res


# --- activity (M3) --------------------------------------------------------------------


def _urls(git: dict[str, Any] | None) -> list[str]:
    out: list[str] = []
    for r in (git or {}).get("repos", []):
        out += [p["url"] for p in r.get("prs", []) if p.get("url")]
        out += [c["url"] for c in r.get("other_commits", []) if c.get("url")]
    return out


def replay_activity(data: dict[str, Any]) -> ContextResult:
    git, cal = data.get("git"), data.get("calendar")
    sources = []
    if git and git.get("totals", {}).get("commits", 0) + git.get("totals", {}).get("prs", 0) > 0:
        sources.append("git")
    if cal and cal.get("count", 0) > 0:
        sources.append("calendar")
    return ContextResult(data=data, meta={"sources": sources, "urls": _urls(git)})


@context_provider(
    "own_activity",
    permission="read:own_activity",
    description="The requesting user's own git commits / PRs and meetings for the date",
    replay=replay_activity,
)
async def own_activity(req: ContextRequest) -> ContextResult:
    date = req.inputs.get("date") or req.run_date
    return replay_activity(await req.relay.activity(req.viewer, req.viewer.user_id, date))


@context_provider(
    "member_activity",
    permission="read:team_activity",
    description="A direct report's git / calendar activity, per source only with their consent",
    replay=replay_activity,
)
async def member_activity(req: ContextRequest) -> ContextResult:
    member, date = req.inputs["member"], req.inputs.get("date") or req.run_date
    return replay_activity(await req.relay.activity(req.viewer, member, date))
