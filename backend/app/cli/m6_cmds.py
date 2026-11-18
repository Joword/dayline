"""M6 commands: DingTalk (bot, org sync, outbox, log import) and log template mapping."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

import typer
from rich.table import Table

from app.bootstrap import Services
from app.cli.main import UserOpt, VerboseOpt, _run_async, _settings, app, err, out
from app.cli.org_cmds import DateOpt, _need_db, _uid

dt_app = typer.Typer(help="DingTalk: bot, org sync, log sync outbox", no_args_is_help=True)
tpl_app = typer.Typer(help="DingTalk log template mapping", no_args_is_help=True)
app.add_typer(dt_app, name="dingtalk")
app.add_typer(tpl_app, name="template")


def _need_dt(s: Services) -> None:
    if s.dingtalk is None:
        err.print(
            "[red]DingTalk is not configured[/red] (DAYLINE_DINGTALK_APP_KEY / DAYLINE_DINGTALK_APP_SECRET)"
        )
        raise typer.Exit(1)


def _state(s: Services):
    from app.dingtalk.templates import template_state

    st = s.settings
    return template_state(st.resolve(st.dingtalk_template_path), st.resolve(st.dingtalk_mapping_path))


# --- dl dingtalk ---------------------------------------------------------------------------


@dt_app.command("status")
def dt_status(
    check_template: Annotated[
        str | None, typer.Option("--check-template", help="DingTalk userid to look up the log template with")
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """配置、令牌、模板映射和 outbox 状态 / configuration and health."""

    async def flow(s: Services) -> None:
        st = s.settings
        out.print(
            f"DingTalk configured: {'yes' if st.dingtalk_enabled else 'no'} · log sync: {st.dingtalk_log_sync} · template: {st.dingtalk_report_template}"
        )
        state = _state(s)
        tpl = (
            "empty (whole report in one field)"
            if state.empty
            else ("mapped" if state.usable else "[red]changed or not mapped - run `dl template map`[/red]")
        )
        out.print(f"template file: {tpl}")
        if s.dingtalk is not None:
            await s.dingtalk.access_token()
            out.print("[green]access token ok[/green]")
            if check_template:
                t = await s.dingtalk.get_template(st.dingtalk_report_template, check_template)
                out.print(
                    f"template '{t.get('name')}' id={t.get('id')} fields={[f.get('field_name') for f in t.get('fields') or []]}"
                )
        if s.outbox is not None:
            counts = {}
            for status in ("pending", "processing", "dead"):
                counts[status] = len(await s.outbox.repo.list(status, limit=1000))
            out.print(f"outbox: {counts}")

    _run_async(flow, verbose=verbose)


@dt_app.command("sync-org")
def dt_sync_org(
    dry_run: Annotated[bool, typer.Option(help="show the changes only")] = False,
    deactivate_missing: Annotated[bool, typer.Option(help="deactivate people no longer in DingTalk")] = False,
    verbose: VerboseOpt = False,
) -> None:
    """从钉钉通讯录同步组织架构 / sync the org tree from the DingTalk directory."""
    from app.dingtalk.orgsync import directory_rows, fetch_directory
    from app.org.importer import apply_plan, build_plan

    async def flow(s: Services) -> None:
        _need_db(s)
        _need_dt(s)
        depts, users = await fetch_directory(s.dingtalk, s.settings.dingtalk_root_dept_id)  # type: ignore[arg-type]
        rows = directory_rows(
            depts, users, await s.users.all(include_inactive=True), s.settings.dingtalk_root_dept_id
        )  # type: ignore[union-attr]
        res = await apply_plan(
            s.users, build_plan(rows), deactivate_missing=deactivate_missing, dry_run=dry_run
        )  # type: ignore[arg-type]
        tag = "[yellow](dry run)[/yellow] " if dry_run else ""
        out.print(
            f"{tag}{len(depts)} departments, {len(users)} people · created {len(res.created)} · updated {len(res.updated)} · deactivated {len(res.deactivated)}"
        )
        for w in res.warnings:
            out.print(f"[yellow]⚠ {w}[/yellow]")

    _run_async(flow, verbose=verbose)


@dt_app.command("bot")
def dt_bot(verbose: VerboseOpt = False) -> None:
    """运行钉钉机器人(Stream 模式,只运行一个实例)/ run the DingTalk Stream bot."""
    from app.core.logging import setup_logging
    from app.dingtalk.stream import run_bot

    settings = _settings(verbose)
    setup_logging(settings.log_level, settings.log_format)

    async def flow(s: Services) -> None:
        _need_db(s)
        _need_dt(s)
        await run_bot(s)

    try:
        _run_async(flow, verbose=verbose)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


@dt_app.command("outbox")
def dt_outbox(
    status: Annotated[str | None, typer.Option(help="pending | processing | done | dead")] = None,
    retry_dead: Annotated[
        bool, typer.Option("--retry-dead", help="re-queue dead items (e.g. after remapping)")
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """查看 / 重试待同步项 / inspect or retry the outbox."""

    async def flow(s: Services) -> None:
        _need_db(s)
        if retry_dead:
            out.print(f"re-queued {await s.outbox.repo.retry_dead()}")  # type: ignore[union-attr]
            return
        t = Table("type", "status", "attempts", "next retry", "last error")
        for d in await s.outbox.repo.list(status):  # type: ignore[union-attr]
            t.add_row(
                d["type"],
                d["status"],
                str(d["attempts"]),
                f"{d['next_retry_at']:%m-%d %H:%M}",
                (d.get("last_error") or "")[:60],
            )
        out.print(t)

    _run_async(flow, verbose=verbose)


@dt_app.command("import-log")
def dt_import_log(date: DateOpt = None, user: UserOpt = None, verbose: VerboseOpt = False) -> None:
    """读取自己当天的钉钉日志作为输入(dl dingtalk import-log | dl gen -f -)/ read your DingTalk log."""
    from app.dingtalk.orgsync import fetch_log_text

    async def flow(s: Services) -> None:
        _need_db(s)
        _need_dt(s)
        u = await s.relay.user(_uid(user))  # type: ignore[union-attr]
        if not u.dingtalk_id:
            err.print(f"[red]{u.id} has no DingTalk account linked[/red]")
            raise typer.Exit(1)
        day = date or s.runner.today().isoformat()
        text = await fetch_log_text(
            s.dingtalk,
            u.dingtalk_id,
            day,
            template_name=s.settings.dingtalk_report_template,
            tz=s.settings.timezone,
        )  # type: ignore[arg-type]
        if not text:
            err.print("no DingTalk log for that day")
            raise typer.Exit(1)
        print(text)

    _run_async(flow, verbose=verbose)


# --- dl template ---------------------------------------------------------------------------


@tpl_app.command("status")
def tpl_status(verbose: VerboseOpt = False) -> None:
    """模板与映射状态 / template and mapping state."""

    async def flow(s: Services) -> None:
        state = _state(s)
        if state.empty:
            out.print("template is empty: the whole report goes into the template's first field")
            return
        out.print(
            f"template hash {state.hash} · mapping {'present' if state.mapping else 'missing'}"
            + (
                " · [red]stale - run `dl template map`[/red]"
                if state.stale
                else " · [green]up to date[/green]"
            )
        )
        if state.mapping:
            for f in state.mapping.fields:
                out.print(f"  {f.dingtalk_field:<12} ← {', '.join(f.source)} ({f.format})")

    _run_async(flow, verbose=verbose, db="off")


@tpl_app.command("fetch")
def tpl_fetch(
    user: Annotated[str, typer.Option("--user", "-u", help="a DingTalk userid who can see the template")],
    verbose: VerboseOpt = False,
) -> None:
    """从钉钉读取日志模板字段,写入 dingtalk_report.md / fetch the template fields from DingTalk."""

    async def flow(s: Services) -> None:
        _need_dt(s)
        st = s.settings
        t = await s.dingtalk.get_template(st.dingtalk_report_template, user)  # type: ignore[union-attr]
        fields = [
            f.get("field_name") for f in sorted(t.get("fields") or [], key=lambda f: int(f.get("sort", 0)))
        ]
        if not fields:
            err.print("[red]template not found or has no fields[/red]")
            raise typer.Exit(1)
        path = st.resolve(st.dingtalk_template_path)
        path.write_text(
            f"<!-- DingTalk log template '{t.get('name')}' ({t.get('id')}) -->\n"
            + "\n".join(f"## {f}" for f in fields)
            + "\n",
            encoding="utf-8",
        )
        out.print(f"wrote {len(fields)} fields to {path}; next: `dl template map`")

    _run_async(flow, verbose=verbose, db="off")


@tpl_app.command("map")
def tpl_map(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="save without asking")] = False,
    user: UserOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """用 template-mapper 生成映射草稿,确认后保存 / draft the mapping with the model, then confirm."""
    import sys

    from app.dingtalk.templates import TemplateMapping, save_mapping, template_field_names

    async def flow(s: Services) -> None:
        state = _state(s)
        if state.empty:
            out.print(
                "the template file is empty - fill config/templates/dingtalk_report.md (or `dl template fetch`) first"
            )
            raise typer.Exit(1)
        inputs = {
            "template": state.template_text,
            "fields": ",".join(template_field_names(state.template_text)) or None,
        }
        result = await s.runner.run("template-mapper", inputs, user_id=_uid(user))
        if result.status != "ok":
            for e in result.errors:
                err.print(f"[red]- {e}[/red]")
            raise typer.Exit(2)
        draft = result.output
        t = Table("DingTalk field", "sources", "format")
        for f in draft["fields"]:
            t.add_row(f["dingtalk_field"], ", ".join(f["source"]), f["format"])
        out.print(t)
        out.print(f"unmapped content: {draft['unmapped_policy']}")
        for n in draft.get("notes") or []:
            out.print(f"[yellow]note: {n}[/yellow]")
        if not yes and not (sys.stdin.isatty() and typer.confirm("save this mapping?", default=False)):
            out.print("not saved")
            return
        mapping = TemplateMapping(
            template_hash=state.hash, fields=draft["fields"], unmapped_policy=draft["unmapped_policy"]
        )
        path = s.settings.resolve(s.settings.dingtalk_mapping_path)
        save_mapping(path, mapping)
        out.print(f"[green]saved {path}[/green]")

    _run_async(flow, verbose=verbose, db="off")


@tpl_app.command("preview")
def tpl_preview(
    of: Annotated[str | None, typer.Option("--of", help="whose report (default: yours)")] = None,
    date: DateOpt = None,
    user: UserOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """预览某份日报同步到钉钉时的字段内容 / preview the DingTalk log contents for a report."""
    from app.dingtalk.templates import render_contents

    async def flow(s: Services) -> None:
        _need_db(s)
        me = _uid(user)
        day = date or s.runner.today().isoformat()
        doc = await s.reports.latest(of or me, day) if (of or me) == me else None  # type: ignore[union-attr]
        if doc is None:
            v = await s.relay.get_report(await s.relay.viewer(me), of or me, day)  # type: ignore[union-attr]
            doc = await s.reports.get(v.id)  # type: ignore[union-attr]
        state = _state(s)
        if not state.usable:
            err.print("[red]template changed or not mapped - run `dl template map`[/red]")
            raise typer.Exit(1)
        contents = render_contents(
            doc["output"],
            None if state.empty else state.mapping,
            lang=doc.get("lang", "zh"),
            default_field="(第一个字段)",
        )
        out.print_json(json.dumps(contents, ensure_ascii=False))

    _run_async(flow, verbose=verbose)
