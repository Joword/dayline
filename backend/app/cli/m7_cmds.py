"""M7 commands: member MCP tools.

dl tools check DIR --url URL       contract test of a running tool (CI and local development)
dl tools submit DIR --definition F register a reviewed build (CI), or --url to read it live
dl tools list | show NAME          states / review view (definition text as the model sees it)
dl tools approve | reject NAME     developers
dl tools enable | disable NAME     after deploying the pinned image / to take it out of use
dl tools verify [NAME...]          live definition still matches the approval?
dl tools compose                   docker-compose.tools.yml + per-tool env files
dl tools egress-proxy              outbound whitelist proxy (runs in its own container)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from app.bootstrap import Services
from app.cli.main import JsonOpt, UserOpt, VerboseOpt, _run_async, _settings, app, err, out
from app.cli.org_cmds import _uid
from app.db.models import ToolDoc
from app.tools.contract import Finding, check_definition, definition_hash, load_examples, run_examples
from app.tools.manifest import ManifestError, load_manifest
from app.tools.service import approvals_needed, current_approvals

tools_app = typer.Typer(help="Member MCP tools: check, submit, review, enable", no_args_is_help=True)
app.add_typer(tools_app, name="tools")

NameArg = Annotated[str, typer.Argument(help="tool name (as in tool.yaml)")]
DirArg = Annotated[Path, typer.Argument(help="tool directory (holding tool.yaml) or the tool.yaml file")]
TokenOpt = Annotated[
    str | None,
    typer.Option("--token", help="bearer the tool expects (its DAYLINE_TOOL_TOKEN); default: none"),
]


def _need_tools(s: Services):
    if s.tool_service is None:
        err.print(f"[red]{s.db_error or 'MongoDB not reachable'}[/red] ({s.settings.mongo_uri})")
        raise typer.Exit(1)
    return s.tool_service


def _secret(s: Services) -> str:
    if s.settings.tools_secret is None:
        err.print("[red]DAYLINE_TOOLS_SECRET is not set[/red] (member tools are off)")
        raise typer.Exit(1)
    return s.settings.tools_secret.get_secret_value()


def _manifest(path: Path):
    try:
        return load_manifest(path)
    except (ManifestError, OSError) as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None


def _print_findings(findings: list[Finding]) -> None:
    for f in findings:
        color = "red" if f.level == "error" else "yellow"
        out.print(f"  [{color}]{f.level}[/{color}] {f.tool or '-'}: {f.message}")


async def _probe(url: str, token: str | None, tool_dir: Path | None, timeout_s: float) -> dict[str, Any]:
    from app.tools.mcp_adapter import list_definition, open_url, result_text

    examples = load_examples(tool_dir) if tool_dir is not None else []
    async with open_url(url, token=token, timeout_s=timeout_s) as c:
        definition = await list_definition(c)

        async def call(tool: str, args: dict[str, Any]) -> tuple[str, bool]:
            r = await c.call_tool(tool, args, read_timeout_seconds=timeout_s)
            return result_text(r), bool(r.is_error)

        ex_findings = await run_examples(examples, definition, call)
    return {"definition": definition, "examples": len(examples), "example_findings": ex_findings}


# --- check / submit ----------------------------------------------------------------------------


@tools_app.command("check")
def tools_check(
    path: DirArg,
    url: Annotated[
        str, typer.Option("--url", help="the running tool's MCP endpoint, e.g. http://localhost:8080/mcp")
    ],
    token: TokenOpt = None,
    write_definition: Annotated[
        Path | None,
        typer.Option("--write-definition", help="save tools/list for `dl tools submit --definition`"),
    ] = None,
    timeout: Annotated[float, typer.Option(help="seconds per request")] = 20.0,
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """契约测试:校验 tool.yaml、tools/list 和示例用例 / contract test of a running tool."""
    _settings(verbose)
    manifest = _manifest(path)
    tool_dir = path if path.is_dir() else path.parent
    try:
        res = asyncio.run(_probe(url, token, tool_dir, timeout))
    except Exception as e:
        err.print(f"[red]cannot talk to {url}:[/red] {type(e).__name__}: {e}")
        raise typer.Exit(1) from None
    definition = res["definition"]
    findings = check_definition(manifest.name, definition) + res["example_findings"]
    h = definition_hash(definition)
    if write_definition is not None:
        write_definition.parent.mkdir(parents=True, exist_ok=True)
        write_definition.write_text(json.dumps(definition, ensure_ascii=False, indent=2), encoding="utf-8")
    errors = [f for f in findings if f.level == "error"]
    if as_json:
        out.print_json(
            json.dumps(
                {
                    "name": manifest.name,
                    "version": manifest.version,
                    "definition_hash": h,
                    "tools": [d["name"] for d in definition],
                    "examples": res["examples"],
                    "findings": [f.__dict__ for f in findings],
                    "ok": not errors,
                },
                ensure_ascii=False,
            )
        )
    else:
        out.print(
            f"[bold]{manifest.name}[/bold] v{manifest.version} · {len(definition)} tools · {res['examples']} examples"
        )
        out.print(f"definition hash: {h}")
        _print_findings(findings)
        out.print("[green]contract ok[/green]" if not errors else f"[red]{len(errors)} error(s)[/red]")
    if errors:
        raise typer.Exit(1)


@tools_app.command("submit")
def tools_submit(
    path: DirArg,
    definition: Annotated[
        Path | None,
        typer.Option("--definition", help="tools/list saved by `dl tools check --write-definition`"),
    ] = None,
    url: Annotated[
        str | None, typer.Option("--url", help="read tools/list from a running instance instead")
    ] = None,
    token: TokenOpt = None,
    image_digest: Annotated[
        str | None, typer.Option("--image-digest", help="sha256:... of the built image (if not in tool.yaml)")
    ] = None,
    user: UserOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """提交审阅(状态变为 pending)/ register a build for review."""
    if (definition is None) == (url is None):
        err.print("[red]give exactly one of --definition or --url[/red]")
        raise typer.Exit(2)
    manifest = _manifest(path)

    async def flow(s: Services) -> None:
        svc = _need_tools(s)
        if definition is not None:
            defs = json.loads(definition.read_text(encoding="utf-8"))
        else:
            defs = (await _probe(url, token, None, 20.0))["definition"]  # type: ignore[arg-type]
        doc, warnings, changed = await svc.submit(
            _uid(user), manifest, definition=defs, image_digest=image_digest
        )
        if not changed:
            out.print(f"[yellow]{doc.name} v{doc.version} is unchanged ({doc.status})[/yellow]")
            return
        out.print(
            f"[green]submitted[/green] {doc.name} v{doc.version} · {doc.definition_hash} · pending review"
        )
        _print_findings(warnings)

    _run_async(flow, verbose=verbose)


# --- review ---------------------------------------------------------------------------------


@tools_app.command("list")
def tools_list(
    all_: Annotated[
        bool, typer.Option("--all", "-a", help="every state (default: enabled tools you can use)")
    ] = False,
    user: UserOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """列出成员工具 / list member tools."""

    async def flow(s: Services) -> None:
        svc = _need_tools(s)
        docs = await svc.repo.list() if all_ else await svc.usable_by(_uid(user))
        t = Table("name", "status", "version", "owner", "visibility", "permissions", "approvals", "tools")
        for d in docs:
            color = {"enabled": "green", "pending": "yellow", "approved": "cyan"}.get(d.status, "red")
            t.add_row(
                d.name,
                f"[{color}]{d.status}[/{color}]",
                d.version,
                d.owner,
                d.visibility,
                ",".join(d.permissions) or "-",
                f"{len(current_approvals(d))}/{approvals_needed(d)}",
                " ".join(f"{d.name}__{x['name']}" for x in d.definition),
            )
        out.print(t)

    _run_async(flow, verbose=verbose)


def _show(d: ToolDoc) -> None:
    out.print(
        f"[bold]{d.name}[/bold] v{d.version} · {d.status}"
        + (f" ({d.disabled_reason})" if d.disabled_reason else "")
    )
    out.print(
        f"owner {d.owner} · {d.language} · visibility {d.visibility} · manager data: {'yes' if d.manager_data else 'no'}"
    )
    out.print(f"image {d.image}@{d.image_digest}")
    out.print(f"endpoint {d.endpoint} · timeout {d.timeout_s}s · resources {d.resources}")
    out.print(f"permissions: {', '.join(d.permissions) or '-'} · egress: {', '.join(d.egress) or '-'}")
    approvals = ", ".join(
        f"{a.by}{' (manager data ack)' if a.manager_data_ack else ''}" for a in current_approvals(d)
    )
    out.print(f"approvals: {len(current_approvals(d))}/{approvals_needed(d)} {approvals}")
    if d.health:
        out.print(
            f"health: {'ok' if d.health.ok else d.health.error} at {d.health.checked_at:%Y-%m-%d %H:%M}"
        )
    out.print(f"definition {d.definition_hash}  [dim](exactly what the model will see)[/dim]")
    for item in d.definition:
        out.print(f"\n  [bold]{d.name}__{item['name']}[/bold]")
        out.print(f"  {item.get('description', '')}")
        out.print(f"  [dim]input: {json.dumps(item.get('inputSchema'), ensure_ascii=False)}[/dim]")
    findings = check_definition(d.name, d.definition)
    if findings:
        out.print("\n[bold]for the reviewer:[/bold]")
        _print_findings(findings)
    out.print("\n[bold]history:[/bold]")
    for e in d.history[-10:]:
        out.print(f"  {e.at:%Y-%m-%d %H:%M} {e.actor_id} {e.action} {e.note or ''}")


@tools_app.command("show")
def tools_show(name: NameArg, as_json: JsonOpt = False, verbose: VerboseOpt = False) -> None:
    """审阅视图:清单、权限、定义原文、风险提示、流转记录 / review view of one tool."""

    async def flow(s: Services) -> None:
        d = await _need_tools(s).get(name)
        if as_json:
            out.print_json(d.model_dump_json())
        else:
            _show(d)

    _run_async(flow, verbose=verbose)


@tools_app.command("approve")
def tools_approve(
    name: NameArg,
    ack_manager_data: Annotated[
        bool, typer.Option("--ack-manager-data", help="required for tools with manager_data: true")
    ] = False,
    user: UserOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """审批通过(developer,不能是负责人本人)/ approve (developers)."""

    async def flow(s: Services) -> None:
        d = await _need_tools(s).approve(_uid(user), name, ack_manager_data=ack_manager_data)
        if d.status == "approved":
            out.print(
                f"[green]approved[/green] {d.name} v{d.version}; deploy it (`make tools-up`), then `dl tools enable {d.name}`"
            )
        else:
            out.print(
                f"approval recorded: {len(current_approvals(d))}/{approvals_needed(d)}; needs another developer"
            )

    _run_async(flow, verbose=verbose)


@tools_app.command("reject")
def tools_reject(
    name: NameArg,
    note: Annotated[str, typer.Option("--note", "-n", help="why")],
    user: UserOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """驳回 / reject (developers)."""

    async def flow(s: Services) -> None:
        d = await _need_tools(s).reject(_uid(user), name, note)
        out.print(f"[yellow]rejected[/yellow] {d.name} v{d.version}")

    _run_async(flow, verbose=verbose)


@tools_app.command("enable")
def tools_enable(name: NameArg, user: UserOpt = None, verbose: VerboseOpt = False) -> None:
    """上线(核对已部署实例的定义与审批一致)/ enable after checking the deployed definition."""

    async def flow(s: Services) -> None:
        d = await _need_tools(s).enable(_uid(user), name)
        out.print(
            f"[green]enabled[/green] {d.name} v{d.version}: "
            + " ".join(f"{d.name}__{x['name']}" for x in d.definition)
        )

    _run_async(flow, verbose=verbose)


@tools_app.command("disable")
def tools_disable(
    name: NameArg,
    reason: Annotated[str, typer.Option("--reason", "-r", help="why")],
    user: UserOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """停用(developer 或负责人)/ take a tool out of use."""

    async def flow(s: Services) -> None:
        d = await _need_tools(s).disable(_uid(user), name, reason)
        out.print(f"[yellow]disabled[/yellow] {d.name}")

    _run_async(flow, verbose=verbose)


@tools_app.command("verify")
def tools_verify(
    names: Annotated[list[str] | None, typer.Argument(help="tools to check (default: all enabled)")] = None,
    verbose: VerboseOpt = False,
) -> None:
    """核对线上定义与审批是否一致(不一致即停用)/ re-check enabled tools now."""

    async def flow(s: Services) -> None:
        svc = _need_tools(s)
        _secret(s)
        results = await svc.verify(names or None)
        if not results:
            out.print("no enabled tools")
        for name, outcome in results:
            color = "green" if outcome == "ok" else "red"
            out.print(f"{name}: [{color}]{outcome}[/{color}]")

    _run_async(flow, verbose=verbose)


# --- deployment ---------------------------------------------------------------------------------


@tools_app.command("compose")
def tools_compose(verbose: VerboseOpt = False) -> None:
    """生成 docker-compose.tools.yml 和各工具的 env 文件 / write the tools compose file."""
    from app.tools.compose import write

    st = _settings(verbose)
    compose_path, env_dir = st.resolve(st.tools_compose_file), st.resolve(st.tools_env_dir)
    try:  # env_file paths in the compose file are relative to it
        env_ref = os.path.relpath(env_dir, compose_path.parent).replace(os.sep, "/")
    except ValueError:
        env_ref = str(env_dir)

    async def flow(s: Services) -> None:
        svc = _need_tools(s)
        secret = _secret(s)
        docs = [d for d in await svc.repo.list(["approved", "enabled"]) if d.image_digest]
        paths = write(docs, secret=secret, compose_path=compose_path, env_dir=env_dir, env_dir_ref=env_ref)
        out.print(f"{len(docs)} tool(s): {', '.join(d.name for d in docs) or '-'}")
        for p in paths:
            out.print(f"  wrote {p}")
        out.print(
            "deploy: make tools-up (docker compose -f docker-compose.yml -f docker-compose.prod.yml "
            "-f docker-compose.tools.yml up -d --remove-orphans)"
        )

    _run_async(flow, verbose=verbose)


@tools_app.command("token")
def tools_token(name: NameArg, verbose: VerboseOpt = False) -> None:
    """打印某个工具的 token(本地调试或手工部署)/ print a tool's own token."""
    from app.tools.tokens import tool_token

    st = _settings(verbose)
    if st.tools_secret is None:
        err.print("[red]DAYLINE_TOOLS_SECRET is not set[/red]")
        raise typer.Exit(1)
    out.print(tool_token(st.tools_secret.get_secret_value(), name))


@tools_app.command("egress-proxy")
def tools_egress_proxy(
    port: Annotated[int, typer.Option(help="listen port")] = 3128,
    rules_file: Annotated[
        Path | None,
        typer.Option("--rules-file", help="JSON {tool: [hosts]} (default: $DAYLINE_EGRESS_RULES)"),
    ] = None,
    upstream: Annotated[
        str | None,
        typer.Option("--upstream", help="upstream proxy URL (default: $DAYLINE_TOOLS_UPSTREAM_PROXY)"),
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """运行成员工具的出网白名单代理 / run the egress whitelist proxy."""
    from app.core.logging import setup_logging
    from app.tools.egress import EgressProxy

    st = _settings(verbose)
    setup_logging(st.log_level, st.log_format)
    if st.tools_secret is None:
        err.print("[red]DAYLINE_TOOLS_SECRET is not set[/red]")
        raise typer.Exit(1)
    raw = (
        rules_file.read_text(encoding="utf-8") if rules_file else os.environ.get("DAYLINE_EGRESS_RULES", "{}")
    )
    rules = json.loads(raw)
    proxy = EgressProxy(
        rules,
        st.tools_secret.get_secret_value(),
        upstream=upstream or os.environ.get("DAYLINE_TOOLS_UPSTREAM_PROXY") or None,
    )

    async def main() -> None:
        server = await proxy.serve("0.0.0.0", port)
        out.print(f"egress proxy on :{port} for {len(rules)} tool(s)")
        async with server:
            await server.serve_forever()

    asyncio.run(main())
