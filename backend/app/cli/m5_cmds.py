"""M5 commands: escalations and notifications."""

from __future__ import annotations

from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from rich.table import Table

from app.bootstrap import Services
from app.cli.main import JsonOpt, UserOpt, VerboseOpt, _run_async, app, err, out
from app.cli.org_cmds import DateOpt, _need_db, _uid

esc_app = typer.Typer(help="Escalation chain: raise, accept, forward, resolve", no_args_is_help=True)
app.add_typer(esc_app, name="escalations")

STATUS = {"open": "跟踪中", "escalated": "已上报", "in_progress": "处理中", "resolved": "已解决"}
NoteOpt = Annotated[str | None, typer.Option("--note", "-n", help="note recorded in the chain")]


def _need_esc(s: Services) -> None:
    _need_db(s)
    if s.escalations is None:
        err.print("[red]escalations are not available[/red]")
        raise typer.Exit(1)


@esc_app.command("list")
def esc_list(
    scope: Annotated[str, typer.Option(help="held (waiting for me) | raised (by me) | involved")] = "held",
    all_: Annotated[bool, typer.Option("--all", help="include resolved")] = False,
    user: UserOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """列出升级事项 / list escalations."""

    async def flow(s: Services) -> None:
        _need_esc(s)
        t = Table("id", "status", "holder", "origin", "date", "item")
        for e in await s.escalations.find_for(_uid(user), scope, include_resolved=all_):  # type: ignore[union-attr]
            t.add_row(
                e.id,
                STATUS.get(e.status, e.status),
                e.current_holder_id,
                e.origin.user_id,
                e.origin.date,
                e.text[:50],
            )
        out.print(t)

    _run_async(flow, verbose=verbose)


@esc_app.command("show")
def esc_show(
    esc_id: str, user: UserOpt = None, as_json: JsonOpt = False, verbose: VerboseOpt = False
) -> None:
    """查看流转记录 / show an escalation and its chain."""

    async def flow(s: Services) -> None:
        _need_esc(s)
        e = await s.escalations.get(_uid(user), esc_id)  # type: ignore[union-attr]
        if as_json:
            out.print_json(e.model_dump_json())
            return
        tz = ZoneInfo(s.settings.timezone)
        out.print(
            f"[bold]{e.text}[/bold]\n{STATUS.get(e.status, e.status)} · 当前处理人 {e.current_holder_id} · 来自 {e.origin.user_id} {e.origin.date}"
        )
        if e.need_from:
            out.print(f"需要:{e.need_from}")
        for ev in e.chain:
            move = f" {ev.from_holder} → {ev.to_holder}" if ev.action == "escalated" else ""
            out.print(
                f"  {ev.at.astimezone(tz):%m-%d %H:%M}  {ev.actor_id:<10} {ev.action}{move}"
                + (f"  “{ev.note}”" if ev.note else "")
            )
        if e.resolution:
            out.print(f"[green]解决方式:{e.resolution}[/green]")

    _run_async(flow, verbose=verbose)


@esc_app.command("raise")
def esc_raise(
    member: Annotated[str, typer.Argument(help="whose report (a direct report, or yourself)")],
    item: Annotated[int, typer.Option("--item", "-i", help="1-based number of the blocker in that report")],
    date: DateOpt = None,
    track: Annotated[bool, typer.Option(help="track at your level only, do not send up")] = False,
    note: NoteOpt = None,
    user: UserOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """把日报中的阻塞项标记为需上级介入 / raise a blocker from a report."""

    async def flow(s: Services) -> None:
        _need_esc(s)
        day = date or s.runner.today().isoformat()
        e = await s.escalations.raise_from_report(
            _uid(user), member, day, item - 1, escalate=not track, note=note
        )  # type: ignore[union-attr]
        out.print(f"{e.id}: {STATUS[e.status]} → {e.current_holder_id}")

    _run_async(flow, verbose=verbose)


def _action(name: str, fn_name: str, needs_note: bool = False):
    def cmd(esc_id: str, note: NoteOpt = None, user: UserOpt = None, verbose: VerboseOpt = False) -> None:
        async def flow(s: Services) -> None:
            _need_esc(s)
            fn = getattr(s.escalations, fn_name)
            e = await (fn(_uid(user), esc_id, note or "") if needs_note else fn(_uid(user), esc_id, note))
            out.print(f"{e.id}: {STATUS[e.status]} · 当前处理人 {e.current_holder_id}")

        _run_async(flow, verbose=verbose)

    cmd.__doc__ = name
    return cmd


esc_app.command("accept")(_action("接手 / take it on", "accept"))
esc_app.command("forward")(_action("继续上报给自己的上级 / pass it one level up", "escalate"))
esc_app.command("resolve")(_action("就地解决(需要 --note)/ resolve with a note", "resolve", needs_note=True))
esc_app.command("comment")(_action("补充信息 / add a comment", "comment", needs_note=True))


@app.command()
def notifications(
    unread: Annotated[bool, typer.Option(help="unread only")] = False,
    read_all: Annotated[bool, typer.Option("--read-all", help="mark everything read")] = False,
    user: UserOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """站内通知 / in-app notifications."""

    async def flow(s: Services) -> None:
        _need_esc(s)
        uid = _uid(user)
        tz = ZoneInfo(s.settings.timezone)
        for n in await s.notifications.for_user(uid, unread_only=unread):  # type: ignore[union-attr]
            mark = " " if n.get("read_at") else "[bold blue]•[/bold blue]"
            out.print(
                f"{mark} {n['created_at'].astimezone(tz):%m-%d %H:%M}  {n['title']}"
                + (f"  [dim]{n['link']}[/dim]" if n.get("link") else "")
            )
        if read_all:
            out.print(f"marked {await s.notifications.mark_read(uid)} read")  # type: ignore[union-attr]

    _run_async(flow, verbose=verbose)
