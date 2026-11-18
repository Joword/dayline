"""M2 commands: org, token, report, team, consent, audit, worker.

The CLI is an operator / developer tool with direct database access; `--user`
picks whose identity a command acts with. End users go through the API.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.markdown import Markdown
from rich.table import Table
from rich.tree import Tree

from app.bootstrap import Services
from app.cli.main import (
    JsonOpt,
    LangOpt,
    UserOpt,
    VerboseOpt,
    _lang,
    _run_async,
    _settings,
    _show,
    app,
    err,
    out,
)
from app.rules.trends import compute_trends

org_app = typer.Typer(help="Org tree (manual import until DingTalk sync in M6)", no_args_is_help=True)
token_app = typer.Typer(help="Personal API tokens", no_args_is_help=True)
report_app = typer.Typer(help="Read reports (through Relay's visibility rules)", no_args_is_help=True)
team_app = typer.Typer(help="Manager: team status, reviews, roll-up", no_args_is_help=True)
consent_app = typer.Typer(help="Share git / calendar data with your direct manager", no_args_is_help=True)
for sub, name in (
    (org_app, "org"),
    (token_app, "token"),
    (report_app, "report"),
    (team_app, "team"),
    (consent_app, "consent"),
):
    app.add_typer(sub, name=name)

DateOpt = Annotated[str | None, typer.Option("--date", "-d", help="YYYY-MM-DD (default today)")]


def _need_db(s: Services) -> None:
    if s.relay is None:
        err.print(f"[red]{s.db_error or 'MongoDB not reachable'}[/red] ({s.settings.mongo_uri})")
        raise typer.Exit(1)


def _uid(user: str | None) -> str:
    return user or _settings(False).user


# --- org ------------------------------------------------------------------------------


@org_app.command("import")
def org_import(
    file: Annotated[Path, typer.Argument(help="org.yaml or org.csv")],
    deactivate_missing: Annotated[bool, typer.Option(help="deactivate active users not in the file")] = False,
    dry_run: Annotated[bool, typer.Option(help="validate and show the changes only")] = False,
    verbose: VerboseOpt = False,
) -> None:
    """导入组织树 / import the org tree (full snapshot)."""
    from app.org.importer import apply_plan, build_plan, parse_file

    async def flow(s: Services) -> None:
        _need_db(s)
        plan = build_plan(parse_file(file))
        res = await apply_plan(s.users, plan, deactivate_missing=deactivate_missing, dry_run=dry_run)  # type: ignore[arg-type]
        tag = "[yellow](dry run)[/yellow] " if dry_run else ""
        out.print(
            f"{tag}created {len(res.created)} · updated {len(res.updated)} · deactivated {len(res.deactivated)}"
        )
        for w in res.warnings:
            out.print(f"[yellow]⚠ {w}[/yellow]")

    _run_async(flow, verbose=verbose)


@org_app.command("show")
def org_show(verbose: VerboseOpt = False) -> None:
    """显示组织树 / print the org tree."""

    async def flow(s: Services) -> None:
        _need_db(s)
        users = await s.users.all()  # type: ignore[union-attr]
        nodes: dict[str, Tree] = {}
        roots = Tree("[bold]org[/bold]")
        for u in sorted(users, key=lambda x: x.org_path):
            label = f"{u.name} [dim]{u.id}[/dim]" + (f" · {u.org_unit}" if u.org_unit else "")
            if u.system_roles:
                label += f" [cyan]{','.join(u.system_roles)}[/cyan]"
            parent = nodes.get(u.manager_id or "", roots)
            nodes[u.id] = parent.add(label)
        out.print(roots)

    _run_async(flow, verbose=verbose)


@org_app.command("grant")
def org_grant(
    user: str, role: Annotated[str, typer.Argument(help="admin | developer")], verbose: VerboseOpt = False
) -> None:
    """授予系统角色 / grant a system role."""
    _set_role(user, role, add=True, verbose=verbose)


@org_app.command("revoke")
def org_revoke(user: str, role: str, verbose: VerboseOpt = False) -> None:
    """撤销系统角色 / revoke a system role."""
    _set_role(user, role, add=False, verbose=verbose)


def _set_role(user: str, role: str, *, add: bool, verbose: bool) -> None:
    if role not in ("admin", "developer"):
        err.print("[red]role must be admin or developer[/red]")
        raise typer.Exit(2)

    async def flow(s: Services) -> None:
        _need_db(s)
        u = await s.users.get(user)  # type: ignore[union-attr]
        if u is None:
            err.print(f"[red]unknown user {user}[/red]")
            raise typer.Exit(1)
        roles = set(u.system_roles) | {role} if add else set(u.system_roles) - {role}
        await s.users.set_roles(user, sorted(roles))  # type: ignore[union-attr]
        out.print(f"{user}: {sorted(roles) or '-'}")

    _run_async(flow, verbose=verbose)


# --- tokens ---------------------------------------------------------------------------


@token_app.command("create")
def token_create(user: str, label: Annotated[str, typer.Option()] = "", verbose: VerboseOpt = False) -> None:
    """为用户创建 API token(只显示一次)/ create a token for a user (shown once)."""
    from app.core.security import hash_token, new_token
    from app.db.models import ApiToken

    async def flow(s: Services) -> None:
        _need_db(s)
        await s.relay.user(user)  # type: ignore[union-attr]
        tid, tok = new_token()
        await s.users.add_token(user, ApiToken(id=tid, hash=hash_token(tok), label=label))  # type: ignore[union-attr]
        out.print(f"token for {user} (id {tid}) - shown once:\n[bold]{tok}[/bold]")

    _run_async(flow, verbose=verbose)


@token_app.command("revoke")
def token_revoke(user: str, token_id: str, verbose: VerboseOpt = False) -> None:
    """撤销 token / revoke a token."""

    async def flow(s: Services) -> None:
        _need_db(s)
        ok = await s.users.revoke_token(user, token_id)  # type: ignore[union-attr]
        out.print("revoked" if ok else "[yellow]no such token[/yellow]")

    _run_async(flow, verbose=verbose)


# --- reports --------------------------------------------------------------------------


@report_app.command("show")
def report_show(
    of: Annotated[str | None, typer.Option("--of", help="whose report (default: your own)")] = None,
    date: DateOpt = None,
    user: UserOpt = None,
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """查看日报 / show a report you are allowed to see."""

    async def flow(s: Services) -> None:
        _need_db(s)
        me = _uid(user)
        v = await s.relay.get_report(await s.relay.viewer(me), of or me, date or s.runner.today().isoformat())  # type: ignore[union-attr]
        if as_json:
            out.print_json(v.model_dump_json())
            return
        out.print(f"[dim]{v.user.name} · v{v.version} · {v.status} · access={v.access}[/dim]")
        out.print(Markdown(v.rendered_md or "(empty)"))

    _run_async(flow, verbose=verbose)


@report_app.command("history")
def report_history(
    of: Annotated[str | None, typer.Option("--of", help="whose reports (default: your own)")] = None,
    days: Annotated[int, typer.Option("--days", "-n", help="number of reports")] = 5,
    user: UserOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """最近的日报和跨日信号(延期、无进展、重复)/ recent reports and cross-day signals."""

    async def flow(s: Services) -> None:
        _need_db(s)
        me = _uid(user)
        views = await s.relay.history(await s.relay.viewer(me), of or me, limit=days)  # type: ignore[union-attr]
        if not views:
            out.print("no submitted reports")
            return
        t = Table("date", "v", "done", "progress", "blockers", "plans")
        for v in views:
            kinds = [i.get("kind") for i in v.own_items]
            t.add_row(
                v.date,
                str(v.version),
                *(str(kinds.count(k)) for k in ("done", "progress", "blocker", "plan")),
            )
        out.print(t)
        dicts = [{"date": v.date, "own_items": v.own_items} for v in views]
        signals = compute_trends(dicts[0], dicts[1:], today=views[0].date)
        for sig in signals:
            out.print(
                f"[yellow]⚠ {sig.type}[/yellow] {sig.text} [dim]({sig.reports} reports: {', '.join(sig.dates)})[/dim]"
            )
        if not signals:
            out.print("[green]no delays, stalls or repeats detected[/green]")

    _run_async(flow, verbose=verbose)


# --- team -----------------------------------------------------------------------------


@team_app.command("status")
def team_status(date: DateOpt = None, user: UserOpt = None, verbose: VerboseOpt = False) -> None:
    """谁提交了、谁没提交 / who has submitted today."""

    async def flow(s: Services) -> None:
        _need_db(s)
        day = date or s.runner.today().isoformat()
        snap = await s.relay.team_snapshot(await s.relay.viewer(_uid(user)), day)  # type: ignore[union-attr]
        t = Table("member", "status", "version", "items", title=f"{snap.manager.name} · {day}")
        for m in snap.members:
            r = m.report
            t.add_row(
                f"{m.user.name} [dim]{m.user.id}[/dim]",
                "[green]submitted[/green]" if r else "[red]missing[/red]",
                str(r.version) if r else "-",
                str(len(r.own_items)) if r else "-",
            )
        out.print(t)

    _run_async(flow, verbose=verbose)


@team_app.command("review")
def team_review(
    member: Annotated[str, typer.Argument(help="direct report's user id")],
    date: DateOpt = None,
    tone: Annotated[str, typer.Option(help="encouraging | direct")] = "encouraging",
    lang: LangOpt = None,
    user: UserOpt = None,
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """审阅直属下属的日报 / review a direct report's report."""
    settings = _settings(verbose)
    lang_ = _lang(lang, settings)

    async def flow(s: Services) -> None:
        _need_db(s)
        result, review_id = await s.report_service.review_member(
            _uid(user), member, date, tone=tone, lang=lang_
        )  # type: ignore[union-attr]
        _show(result, as_json=as_json, lang=lang_)
        if result.status != "ok":
            raise typer.Exit(2)
        if not as_json:
            out.print(f"[dim]review saved: {review_id}[/dim]")

    _run_async(flow, verbose=verbose)


@team_app.command("rollup")
def team_rollup(
    date: DateOpt = None,
    escalate: Annotated[
        str | None, typer.Option(help="candidates to send up, e.g. '1,3' (asked if omitted)")
    ] = None,
    forward: Annotated[
        str | None, typer.Option(help="escalation ids waiting for you to pass further up, comma-separated")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="confirm without asking")] = False,
    draft_only: Annotated[bool, typer.Option("--draft-only", help="save the draft, do not confirm")] = False,
    lang: LangOpt = None,
    user: UserOpt = None,
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """生成团队汇总草稿并确认 / draft the team roll-up, then confirm it."""
    import sys

    settings = _settings(verbose)
    lang_ = _lang(lang, settings)
    interactive = sys.stdin.isatty() and not yes and not as_json

    async def flow(s: Services) -> None:
        _need_db(s)
        me = _uid(user)
        result, draft, warnings = await s.report_service.draft_rollup(me, date, lang=lang_)  # type: ignore[union-attr]
        _show(result, as_json=as_json, lang=lang_)
        if draft is None:
            raise typer.Exit(2)
        for w in warnings:
            err.print(f"[yellow]⚠ {w}[/yellow]")
        if draft_only or (
            interactive
            and not typer.confirm(
                "确认并发送给上级?" if lang_ == "zh" else "Confirm and send up?", default=False
            )
        ):
            out.print(f"[dim]draft saved: {draft['id']} (v{draft['version']})[/dim]")
            return
        picks = escalate
        n = len(draft["team_summary"].get("escalation_candidates") or [])
        if picks is None and interactive and n:
            picks = typer.prompt(
                "上报哪几条?(如 1,3;回车不上报)"
                if lang_ == "zh"
                else "Escalate which? (e.g. 1,3; Enter for none)",
                default="",
                show_default=False,
            )
        indexes = [int(x) for x in (picks or "").replace(" ", "").split(",") if x]
        pending = draft["team_summary"].get("pending_escalations") or []
        fwd_ids = [x for x in (forward or "").replace(" ", "").split(",") if x]
        if forward is None and interactive and pending:
            for n, p in enumerate(pending, 1):
                out.print(f"  {n}. {p['text']} [dim]({p['status']}, {p['escalation_id']})[/dim]")
            ans = typer.prompt(
                "继续上报哪几条收到的事项?(如 1,2;回车不上报)"
                if lang_ == "zh"
                else "Forward which? (Enter for none)",
                default="",
                show_default=False,
            )
            fwd_ids = [pending[int(x) - 1]["escalation_id"] for x in ans.replace(" ", "").split(",") if x]
        doc, stale = await s.report_service.confirm_rollup(me, draft["id"], escalate=indexes, forward=fwd_ids)  # type: ignore[union-attr]

        if stale:
            err.print(f"[yellow]⚠ updated their report after the draft: {', '.join(stale)}[/yellow]")
        out.print(f"[green]confirmed {doc['date']} v{doc['version']} · escalations: {len(indexes)}[/green]")

    _run_async(flow, verbose=verbose)


# --- consent ----------------------------------------------------------------------------


@consent_app.command("grant")
def consent_grant(
    scope: Annotated[str, typer.Argument(help="git | calendar")],
    user: UserOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """允许直属上级用你的 git / 日历数据做交叉验证 / let your manager cross-check with this data."""

    async def flow(s: Services) -> None:
        _need_db(s)
        c = await s.relay.grant_consent(await s.relay.viewer(_uid(user)), scope)  # type: ignore[union-attr]
        out.print(f"granted {c.scope} to {c.grantee_id}")

    _run_async(flow, verbose=verbose)


@consent_app.command("revoke")
def consent_revoke(scope: str, user: UserOpt = None, verbose: VerboseOpt = False) -> None:
    """撤销授权 / revoke."""

    async def flow(s: Services) -> None:
        _need_db(s)
        ok = await s.relay.revoke_consent(await s.relay.viewer(_uid(user)), scope)  # type: ignore[union-attr]
        out.print("revoked" if ok else "nothing to revoke")

    _run_async(flow, verbose=verbose)


@consent_app.command("list")
def consent_list(user: UserOpt = None, verbose: VerboseOpt = False) -> None:
    """查看授权 / list."""

    async def flow(s: Services) -> None:
        _need_db(s)
        for c in await s.relay.consents(await s.relay.viewer(_uid(user))):  # type: ignore[union-attr]
            out.print(f"{c.scope} -> {c.grantee_id} (since {c.granted_at:%Y-%m-%d})")

    _run_async(flow, verbose=verbose)


# --- audit / worker -----------------------------------------------------------------------


@app.command()
def audit(
    actor: Annotated[str | None, typer.Option(help="filter by actor")] = None,
    limit: Annotated[int, typer.Option()] = 30,
    verbose: VerboseOpt = False,
) -> None:
    """查看访问审计日志 / show the access audit log."""

    from zoneinfo import ZoneInfo

    async def flow(s: Services) -> None:
        _need_db(s)
        tz = ZoneInfo(s.settings.timezone)
        t = Table("time", "actor", "client", "action", "target", "allowed", "reason")
        for e in await s.audit.recent(actor_id=actor, limit=limit):  # type: ignore[union-attr]
            t.add_row(
                f"{e['ts'].astimezone(tz):%m-%d %H:%M:%S}",
                e["actor_id"],
                e["client_id"],
                e["action"],
                e["target"],
                "✓" if e["allowed"] else "[red]✗[/red]",
                e.get("reason") or "",
            )
        out.print(t)

    _run_async(flow, verbose=verbose)


@app.command()
def worker(
    once: Annotated[bool, typer.Option(help="run due jobs once and exit")] = False,
    verbose: VerboseOpt = False,
) -> None:
    """后台任务:截止提醒、团队汇总草稿 / background jobs."""
    from app.core.logging import setup_logging
    from app.jobs.worker import Worker

    settings = _settings(verbose)
    setup_logging(settings.log_level, settings.log_format)

    async def flow(s: Services) -> None:
        _need_db(s)
        w = Worker(s)
        if once:
            ran, delivered = [], []
            if await w.acquire_lease():
                ran = await w.run_due()
                delivered = await w.process_outbox()
            out.print(json.dumps({"ran": ran, "outbox": delivered}, ensure_ascii=False))
            return
        await w.run_forever()

    try:
        _run_async(flow, verbose=verbose)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
