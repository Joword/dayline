"""M3 commands: integrations, activity, ask (intent routing), collector.

Secrets are never taken as plain command-line arguments (they would land in shell
history): they are read from a hidden prompt or from an environment variable named
with ``--*-env``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

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
from app.cli.org_cmds import DateOpt, _need_db, _uid

integ_app = typer.Typer(
    help="Git / calendar connections (credentials stored encrypted)", no_args_is_help=True
)
collector_app = typer.Typer(
    help="Intranet collector (pushes activity summaries to the main system)", no_args_is_help=True
)
app.add_typer(integ_app, name="integrations")
app.add_typer(collector_app, name="collector")


def _secret(env: str | None, prompt: str) -> str:
    if env:
        v = os.environ.get(env)
        if not v:
            err.print(f"[red]env var {env} is empty[/red]")
            raise typer.Exit(2)
        return v
    return typer.prompt(prompt, hide_input=True)


def _need_crypto(s: Services) -> None:
    if s.activity is None or not s.activity.crypto.enabled:
        err.print("[red]DAYLINE_ENCRYPTION_KEY is not set[/red] - generate one with `dl integrations keygen`")
        raise typer.Exit(1)


@integ_app.command("keygen")
def keygen() -> None:
    """生成加密密钥(放进 DAYLINE_ENCRYPTION_KEY,与数据库备份分开保管)/ new encryption key."""
    from app.core.crypto import generate_key

    out.print(generate_key())


@integ_app.command("add-git")
def add_git(
    provider: Annotated[str, typer.Option(help="github | gitlab")],
    user: UserOpt = None,
    base_url: Annotated[
        str | None, typer.Option(help="GitHub Enterprise API URL / self-hosted GitLab URL")
    ] = None,
    username: Annotated[str | None, typer.Option(help="GitHub login (required for GitHub)")] = None,
    email: Annotated[list[str] | None, typer.Option(help="commit author email(s), repeatable")] = None,
    token_env: Annotated[
        str | None, typer.Option(help="read the token from this env var instead of a prompt")
    ] = None,
    proxy: Annotated[str, typer.Option(help="auto | none | proxy URL")] = "auto",
    ca_bundle: Annotated[str | None, typer.Option(help="company CA bundle path")] = None,
    verbose: VerboseOpt = False,
) -> None:
    """连接 GitHub / GitLab(只读 token)/ connect a git account (read-only token)."""
    from app.integrations.models import GitConfig

    token = _secret(token_env, "read-only token")

    async def flow(s: Services) -> None:
        _need_db(s)
        _need_crypto(s)
        uid = _uid(user)
        await s.relay.user(uid)  # type: ignore[union-attr]
        cfg = GitConfig(
            provider=provider,
            base_url=base_url,
            username=username,
            emails=email or [],
            token=token,
            proxy=proxy,
            ca_bundle=ca_bundle,
        )  # type: ignore[arg-type]
        doc = await s.activity.add(uid, cfg)  # type: ignore[union-attr]
        out.print(f"added {doc.label} ({doc.id}) for {uid} - test with `dl activity -u {uid} --refresh`")

    _run_async(flow, verbose=verbose)


@integ_app.command("add-calendar")
def add_calendar(
    provider: Annotated[str, typer.Option(help="outlook | coremail")],
    user: UserOpt = None,
    mode: Annotated[
        str, typer.Option(help="auto | ics | caldav | imap (coremail) / auto | ics (outlook)")
    ] = "auto",
    email: Annotated[str | None, typer.Option(help="your own address (to read your attendee status)")] = None,
    ics_url_env: Annotated[
        str | None, typer.Option(help="env var holding a published ICS URL (secret)")
    ] = None,
    caldav_url: Annotated[str | None, typer.Option()] = None,
    imap_host: Annotated[str | None, typer.Option()] = None,
    username: Annotated[str | None, typer.Option()] = None,
    password_env: Annotated[
        str | None, typer.Option(help="env var with the client-specific password")
    ] = None,
    proxy: Annotated[str, typer.Option()] = "auto",
    ca_bundle: Annotated[str | None, typer.Option()] = None,
    verbose: VerboseOpt = False,
) -> None:
    """连接日历。Coremail 只接受客户端专用密码 / 授权码,不要用邮箱主密码 / connect a calendar."""
    from app.integrations.models import CalendarConfig

    ics_url = os.environ.get(ics_url_env) if ics_url_env else None
    password = None
    if caldav_url or imap_host:
        password = _secret(
            password_env, "client-specific password / authorization code (NOT the mailbox password)"
        )

    async def flow(s: Services) -> None:
        _need_db(s)
        _need_crypto(s)
        uid = _uid(user)
        await s.relay.user(uid)  # type: ignore[union-attr]
        cfg = CalendarConfig(
            provider=provider,
            mode=mode,
            email=email,
            ics_url=ics_url,
            caldav_url=caldav_url,
            imap_host=imap_host,  # type: ignore[arg-type]
            username=username,
            password=password,
            proxy=proxy,
            ca_bundle=ca_bundle,  # type: ignore[arg-type]
        )
        doc = await s.activity.add(uid, cfg)  # type: ignore[union-attr]
        out.print(f"added {doc.label} ({doc.id}) for {uid}")

    _run_async(flow, verbose=verbose)


@integ_app.command("connect-outlook")
def connect_outlook(
    user: UserOpt = None,
    email: Annotated[str | None, typer.Option()] = None,
    ics_url_env: Annotated[str | None, typer.Option(help="optional ICS fallback URL (env var)")] = None,
    verbose: VerboseOpt = False,
) -> None:
    """用设备码登录 Microsoft 365,读取 Outlook 日历(只读)/ sign in to Microsoft 365 (device code)."""
    from app.integrations.calendar import graph_device_code_poll, graph_device_code_start
    from app.integrations.http import TargetClient
    from app.integrations.models import CalendarConfig, GraphCreds

    settings = _settings(verbose)
    if not settings.graph_client_id:
        err.print(
            "[red]DAYLINE_GRAPH_CLIENT_ID is not set[/red] (an Azure app registration with Calendars.Read)"
        )
        raise typer.Exit(1)

    async def flow(s: Services) -> None:
        _need_db(s)
        _need_crypto(s)
        uid = _uid(user)
        await s.relay.user(uid)  # type: ignore[union-attr]
        http = TargetClient("auto", fallback_proxy=settings.integration_proxy)
        try:
            flow_ = await graph_device_code_start(http, settings.graph_tenant, settings.graph_client_id)  # type: ignore[arg-type]
            out.print(
                flow_.get("message") or f"open {flow_['verification_uri']} and enter {flow_['user_code']}"
            )
            refresh = await graph_device_code_poll(
                http, settings.graph_tenant, settings.graph_client_id, flow_
            )  # type: ignore[arg-type]
        finally:
            await http.aclose()
        cfg = CalendarConfig(
            provider="outlook",
            email=email,
            ics_url=os.environ.get(ics_url_env) if ics_url_env else None,
            graph=GraphCreds(
                tenant=settings.graph_tenant, client_id=settings.graph_client_id, refresh_token=refresh
            ),  # type: ignore[arg-type]
        )
        doc = await s.activity.add(uid, cfg)  # type: ignore[union-attr]
        out.print(f"[green]connected[/green] {doc.label} ({doc.id})")

    _run_async(flow, verbose=verbose)


@integ_app.command("list")
def integ_list(user: UserOpt = None, verbose: VerboseOpt = False) -> None:
    """查看已连接的数据源 / list connections."""

    async def flow(s: Services) -> None:
        _need_db(s)
        t = Table("id", "type", "label", "status", "mode", "last ok", "last error")
        for d in await s.integrations.for_user(_uid(user)):  # type: ignore[union-attr]
            t.add_row(
                d.id,
                d.type,
                d.label,
                d.status,
                d.state.get("mode_used") or "-",
                f"{d.last_ok_at:%m-%d %H:%M}" if d.last_ok_at else "-",
                (d.last_error or "")[:60],
            )
        out.print(t)

    _run_async(flow, verbose=verbose)


@integ_app.command("remove")
def integ_remove(integration_id: str, user: UserOpt = None, verbose: VerboseOpt = False) -> None:
    """删除连接(吊销凭证)/ remove a connection."""

    async def flow(s: Services) -> None:
        _need_db(s)
        ok = await s.integrations.remove(_uid(user), integration_id)  # type: ignore[union-attr]
        out.print("removed" if ok else "[yellow]not found[/yellow]")

    _run_async(flow, verbose=verbose)


# --- activity / ask ---------------------------------------------------------------------


@app.command()
def activity(
    of: Annotated[str | None, typer.Option("--of", help="a direct report (needs their consent)")] = None,
    date: DateOpt = None,
    refresh: Annotated[bool, typer.Option(help="ignore the cache (own data only)")] = False,
    user: UserOpt = None,
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """当天的 git / 日历活动(经过 Relay 权限检查)/ git and calendar activity."""

    async def flow(s: Services) -> None:
        _need_db(s)
        me = _uid(user)
        day = date or s.runner.today().isoformat()
        a = await s.relay.activity(await s.relay.viewer(me), of or me, day, refresh=refresh)  # type: ignore[union-attr]
        if as_json:
            out.print_json(json.dumps(a, ensure_ascii=False, default=str))
            return
        git, cal = a.get("git"), a.get("calendar")
        if git:
            t = Table("repo", "commits", "PRs / MRs", title=f"git · {day}")
            for r in git.get("repos", []):
                prs = ", ".join(f"#{p['number']} {p['title'][:30]} ({p['state']})" for p in r["prs"])
                t.add_row(r["repo"], str(r["commit_count"]), prs or "-")
            out.print(t)
        if cal:
            t = Table(
                "time", "meeting", "min", "people", title=f"calendar · {day} · {cal['total_minutes']} min"
            )
            for e in cal.get("events", []):
                t.add_row(f"{e['start']}-{e['end']}", e["title"], str(e["minutes"]), str(e["attendees"]))
            out.print(t)
        for src, why in {**a.get("unavailable", {}), **a.get("errors", {})}.items():
            out.print(f"[yellow]{src}: {why}[/yellow]")
        if not git and not cal and not a.get("unavailable") and not a.get("errors"):
            out.print("no git / calendar connected (dl integrations add-git / add-calendar)")

    _run_async(flow, verbose=verbose)


@app.command()
def ask(
    text: Annotated[str, typer.Argument(help="what you want, in your own words (or a /command)")],
    lang: LangOpt = None,
    user: UserOpt = None,
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """用自然语言调用 skill(先匹配命令,再由小模型识别意图)/ route free text to a skill."""
    from app.skills.router import primary_text_input

    settings = _settings(verbose)
    lang_ = _lang(lang, settings)

    async def flow(s: Services) -> None:
        uid = _uid(user)
        is_manager = bool(s.relay) and await s.relay.is_manager(uid)  # type: ignore[union-attr]
        r = await s.router.resolve(text, s.gateway, lang=lang_, is_manager=is_manager)
        if r is None:
            err.print("no matching skill (dl skills list)")
            raise typer.Exit(1)
        field = primary_text_input(r.skill)
        if field is None:
            out.print(f"→ {r.skill.name}: needs structured inputs; use `dl run {r.skill.name} -i ...`")
            return
        if not as_json:
            out.print(f"[dim]→ {r.skill.name} (via {r.via}, {r.confidence:.2f})[/dim]")
        result = await s.runner.run(r.skill.name, {field: r.rest}, user_id=uid, lang=lang_)
        _show(result, as_json=as_json, lang=lang_)

    _run_async(flow, verbose=verbose)


# --- collector ----------------------------------------------------------------------------


@collector_app.command("push")
def collector_push(
    config: Annotated[Path, typer.Option("--config", "-c", help="collector.yaml")],
    date: DateOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """采集内网数据源并把摘要推送给主系统 / collect intranet sources and push summaries."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.integrations.collector import collect_and_push, load_collector_config

    settings = _settings(verbose)
    cfg = load_collector_config(config)
    day = date or datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
    report = asyncio.run(collect_and_push(cfg, day))
    for r in report:
        mark = "[green]pushed[/green]" if r["pushed"] else "[red]not pushed[/red]"
        out.print(f"{r['user_id']} {r['source']}: {mark} {'; '.join(r['errors'])}")
    if any(not r["pushed"] for r in report):
        raise typer.Exit(1)
