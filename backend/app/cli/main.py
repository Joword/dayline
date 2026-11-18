"""`dl` - Dayline command line.

dl gen                      write today's report (editor opens if no text given)
dl review report.md         review someone else's report
dl run <skill> -i k=v       run any skill
dl skills list              list skills and validation problems
dl llm status | probe       endpoint configuration / live feature probe
dl db init                  create MongoDB indexes
dl tools ...                member MCP tools: check, submit, review, enable (m7_cmds)
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import click
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from app.bootstrap import Services, build_services
from app.core.config import Settings, get_settings
from app.core.crypto import CryptoError
from app.core.logging import setup_logging
from app.db.repositories.reports import ReportConflict
from app.escalations.service import EscalationError, EscalationForbidden
from app.integrations.models import IntegrationError
from app.llm.types import NoEndpointAvailable
from app.org.importer import OrgImportError
from app.relay.service import RelayForbidden, RelayNotFound
from app.reports.service import ReportError, ReportForbidden
from app.skills.registry import SkillNotFound
from app.skills.runner import SkillForbidden, SkillInputError, SkillRunError, SkillRunResult
from app.tools.manifest import ManifestError
from app.tools.service import ToolError, ToolForbidden

app = typer.Typer(
    help="Dayline 日报助手 / daily-report assistant", no_args_is_help=True, add_completion=False
)
skills_app = typer.Typer(help="Skills", no_args_is_help=True)
llm_app = typer.Typer(help="LLM endpoints", no_args_is_help=True)
db_app = typer.Typer(help="Database", no_args_is_help=True)
app.add_typer(skills_app, name="skills")
app.add_typer(llm_app, name="llm")
app.add_typer(db_app, name="db")

out = Console()
err = Console(stderr=True)

LangOpt = Annotated[str | None, typer.Option("--lang", "-l", help="zh | en (default: DAYLINE_DEFAULT_LANG)")]
UserOpt = Annotated[
    str | None, typer.Option("--user", "-u", help="user id (default: DAYLINE_USER / OS user)")
]
JsonOpt = Annotated[bool, typer.Option("--json", help="print the raw result as JSON")]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="show logs")]

MSG = {
    "zh": {
        "editor": "# 在下面写今天做了什么(以 # 开头的行会被忽略),保存并关闭编辑器即可。\n",
        "empty": "没有输入内容,已取消。",
        "no_db": "MongoDB 不可用,本次结果不会保存。",
        "answer": "回答(直接回车跳过)",
        "questions": "还有几处信息需要补充:",
        "confirm": "确认这份日报?",
        "saved": "已保存草稿",
        "confirmed": "已确认",
        "invalid": "输出没有通过校验:",
        "cost": "接入点 {endpoint} · 模型 {model} · tokens {tin}/{tout} · 费用 {cost} · 耗时 {secs:.1f}s",
    },
    "en": {
        "editor": "# Write what you did today below (lines starting with # are ignored), then save and close.\n",
        "empty": "No input; cancelled.",
        "no_db": "MongoDB is unavailable; this result will not be saved.",
        "answer": "Answer (Enter to skip)",
        "questions": "A few things need more detail:",
        "confirm": "Confirm this report?",
        "saved": "Draft saved",
        "confirmed": "Confirmed",
        "invalid": "The output failed validation:",
        "cost": "endpoint {endpoint} · model {model} · tokens {tin}/{tout} · cost {cost} · {secs:.1f}s",
    },
}


def _settings(verbose: bool) -> Settings:
    s = get_settings()
    setup_logging("DEBUG" if verbose else "WARNING", "text")
    return s


def _run_async(fn: Callable[[Services], Awaitable[Any]], *, verbose: bool, db: str = "cli") -> Any:
    settings = _settings(verbose)

    async def main() -> Any:
        services = await build_services(settings, db=db)  # type: ignore[arg-type]
        try:
            return await fn(services)
        finally:
            await services.aclose()

    try:
        return asyncio.run(main())
    except SkillNotFound as e:
        err.print(f"[red]unknown skill: {e.args[0]}[/red]  (dl skills list)")
    except SkillInputError as e:
        err.print(f"[red]invalid input:[/red] {e}")
    except SkillRunError as e:
        err.print(f"[red]run failed ({e.status}):[/red] {e}\n[dim]run_id={e.run_id}[/dim]")
    except NoEndpointAvailable as e:
        err.print(f"[red]{e}[/red]")
    except (SkillForbidden, RelayForbidden, ReportForbidden, EscalationForbidden, ToolForbidden) as e:
        err.print(f"[red]not allowed:[/red] {e}")
    except ToolError as e:
        err.print(f"[red]{e}[/red]")
        for f in e.findings:
            err.print(f"  - {f}")
    except (
        RelayNotFound,
        ReportError,
        ReportConflict,
        OrgImportError,
        IntegrationError,
        CryptoError,
        EscalationError,
        ManifestError,
    ) as e:
        err.print(f"[red]{e}[/red]")
    except FileNotFoundError as e:
        err.print(f"[red]{e}[/red]")
    raise typer.Exit(1)


def _lang(lang: str | None, settings: Settings) -> str:
    lang = lang or settings.default_lang
    if lang not in MSG:
        err.print(f"[red]unsupported language: {lang}[/red]")
        raise typer.Exit(2)
    return lang


def _read_text(text: str | None, file: Path | None, *, lang: str, editor: bool) -> str:
    if text:
        return text
    if file is not None:
        return sys.stdin.read() if str(file) == "-" else file.read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    if editor:
        edited = click.edit(MSG[lang]["editor"], extension=".md")
        if edited:
            return "\n".join(
                line for line in edited.splitlines() if not line.lstrip().startswith("#")
            ).strip()
    return ""


def _show(result: SkillRunResult, *, as_json: bool, lang: str) -> None:
    if as_json:
        out.print_json(result.model_dump_json())
        return
    if result.status != "ok":
        err.print(f"[red]{MSG[lang]['invalid']}[/red]")
        for e in result.errors:
            err.print(f"  - {e}")
        return
    out.print(Markdown(result.markdown))
    if result.notice:
        out.print(f"[yellow]⚠ {result.notice}[/yellow]")
    for w in result.warnings:
        out.print(f"[yellow]⚠ {w}[/yellow]")
    cost = f"${result.cost_usd:.4f}" if result.cost_usd is not None else "-"
    out.print(
        "[dim]"
        + MSG[lang]["cost"].format(
            endpoint=result.endpoint,
            model=result.model,
            tin=result.usage.input_tokens,
            tout=result.usage.output_tokens,
            cost=cost,
            secs=result.latency_ms / 1000,
        )
        + "[/dim]"
    )


def _parse_kv(pairs: list[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for p in pairs:
        if "=" not in p:
            raise typer.BadParameter(f"expected key=value, got '{p}'")
        k, v = p.split("=", 1)
        if v.startswith("@"):
            v = sys.stdin.read() if v == "@-" else Path(v[1:]).read_text(encoding="utf-8")
        values[k.strip()] = v
    return values


# --- dl gen -------------------------------------------------------------------


@app.command()
def gen(
    text: Annotated[str | None, typer.Argument(help="what you did today (omit to open an editor)")] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="read the account from a file ('-' = stdin)")
    ] = None,
    date: Annotated[
        str | None, typer.Option("--date", "-d", help="report date YYYY-MM-DD (default today)")
    ] = None,
    lang: LangOpt = None,
    user: UserOpt = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="no questions; confirm the report right away")
    ] = False,
    no_save: Annotated[bool, typer.Option("--no-save", help="do not store the report")] = False,
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """生成日报 / write a daily report."""
    settings = _settings(verbose)
    lang_ = _lang(lang, settings)
    narrative = _read_text(text, file, lang=lang_, editor=True)
    if not narrative.strip():
        err.print(MSG[lang_]["empty"])
        raise typer.Exit(1)
    interactive = sys.stdin.isatty() and not yes and not as_json

    async def flow(s: Services) -> None:
        uid = user or settings.user
        if s.reports is None and not no_save:
            err.print(f"[yellow]{MSG[lang_]['no_db']}[/yellow]")
        inputs: dict[str, Any] = {"narrative": narrative, "date": date}
        result = await s.runner.run("generate-daily", inputs, user_id=uid, lang=lang_)
        answered: list[str] = []
        for _ in range(2):  # at most two rounds of follow-up questions
            qs = (result.output or {}).get("questions") or [] if result.status == "ok" else []
            if not (interactive and qs):
                break
            _show(result, as_json=False, lang=lang_)
            out.print(f"\n[bold]{MSG[lang_]['questions']}[/bold]")
            new = []
            for q in qs:
                a = typer.prompt(f"  {q}\n  {MSG[lang_]['answer']}", default="", show_default=False).strip()
                if a:
                    new.append(f"Q: {q}\nA: {a}")
            if not new:
                break
            answered += new
            result = await s.runner.run(
                "generate-daily", {**inputs, "answers": "\n\n".join(answered)}, user_id=uid, lang=lang_
            )
        _show(result, as_json=as_json, lang=lang_)
        if result.status != "ok":
            raise typer.Exit(2)
        if no_save or s.reports is None:
            return
        doc = await s.reports.save_draft(
            user_id=uid,
            date=result.output["date"],
            lang=lang_,
            raw_input={"narrative": narrative, "answers": "\n\n".join(answered) or None},
            output=result.output,
            rendered_md=result.markdown,
            skill_name=result.skill,
            skill_version=result.version,
            skill_run_id=result.run_id,
        )
        msg = f"{MSG[lang_]['saved']}: {doc['date']} v{doc['version']} ({doc['id']})"
        if yes or (interactive and typer.confirm(MSG[lang_]["confirm"], default=True)):
            if (
                s.report_service is not None
            ):  # also alerts the manager if their roll-up cited an older version
                await s.report_service.confirm(uid, doc["id"])
            else:
                await s.reports.confirm(doc["id"])
            msg += f" · {MSG[lang_]['confirmed']}"
        if not as_json:
            out.print(f"[green]{msg}[/green]")

    _run_async(flow, verbose=verbose)


# --- dl review ----------------------------------------------------------------


@app.command()
def review(
    file: Annotated[Path | None, typer.Argument(help="report file ('-' = stdin)")] = None,
    text: Annotated[str | None, typer.Option("--text", "-t", help="report text")] = None,
    author: Annotated[
        str | None, typer.Option("--author", "-a", help="author's name, for the feedback draft")
    ] = None,
    context: Annotated[
        str | None, typer.Option("--context", "-c", help="background, e.g. the author's role")
    ] = None,
    lang: LangOpt = None,
    user: UserOpt = None,
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """审阅别人的日报 / review someone else's report."""
    settings = _settings(verbose)
    lang_ = _lang(lang, settings)
    report = _read_text(text, file, lang=lang_, editor=False)
    if not report.strip():
        err.print(MSG[lang_]["empty"])
        raise typer.Exit(1)

    async def flow(s: Services) -> None:
        inputs = {"report": report, "author": author, "context": context}
        result = await s.runner.run("review-member", inputs, user_id=user or settings.user, lang=lang_)
        _show(result, as_json=as_json, lang=lang_)
        if result.status != "ok":
            raise typer.Exit(2)

    _run_async(flow, verbose=verbose)


# --- dl run -------------------------------------------------------------------


@app.command()
def run(
    skill: Annotated[str, typer.Argument(help="skill name")],
    inputs: Annotated[
        list[str] | None, typer.Option("--input", "-i", help="key=value; key=@file reads a file")
    ] = None,
    lang: LangOpt = None,
    user: UserOpt = None,
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """运行任意 skill / run any skill."""
    settings = _settings(verbose)
    lang_ = _lang(lang, settings)
    values = _parse_kv(inputs or [])

    async def flow(s: Services) -> None:
        result = await s.runner.run(skill, values, user_id=user or settings.user, lang=lang_)
        _show(result, as_json=as_json, lang=lang_)
        if result.status != "ok":
            raise typer.Exit(2)

    _run_async(flow, verbose=verbose, db="cli")


# --- dl skills ------------------------------------------------------------------


@skills_app.command("list")
def skills_list(as_json: JsonOpt = False, verbose: VerboseOpt = False) -> None:
    """列出所有 skill / list skills."""

    async def flow(s: Services) -> None:
        skills = s.registry.list()
        if as_json:
            out.print_json(
                json.dumps(
                    {
                        "skills": [
                            {
                                "name": k.name,
                                "layer": k.layer,
                                **k.manifest.model_dump(mode="json", by_alias=True),
                            }
                            for k in skills
                        ],
                        "problems": [p.__dict__ for p in s.registry.problems],
                    },
                    ensure_ascii=False,
                )
            )
            return
        t = Table("name", "layer", "version", "model", "tools", "langs", "commands", "description")
        for k in skills:
            m = k.manifest
            t.add_row(
                k.name,
                k.layer,
                m.version,
                m.model,
                ",".join(m.tools) or "-",
                ",".join(k.langs),
                " ".join(m.triggers.commands) or "-",
                m.description,
            )
        out.print(t)
        for p in s.registry.problems:
            err.print(f"[red]✗ {p.layer}:{p.path}[/red] {p.error}")

    _run_async(flow, verbose=verbose, db="off")


# --- dl llm -------------------------------------------------------------------


@llm_app.command("status")
def llm_status(verbose: VerboseOpt = False) -> None:
    """显示接入点配置 / show endpoint configuration."""

    async def flow(s: Services) -> None:
        t = Table("#", "endpoint", "adapter", "small", "large", "max level", "features", "status")
        for i, ep in enumerate(s.gateway.config.ordered(), 1):
            feats = ",".join(k for k, v in ep.features.model_dump().items() if v)
            reason = ep.unavailable_reason()
            t.add_row(
                str(i),
                ep.name,
                ep.adapter,
                ep.model_for("small") or "TODO",
                ep.model_for("large") or "TODO",
                ep.max_data_level,
                feats or "-",
                "[green]ready[/green]" if reason is None else f"[yellow]{reason}[/yellow]",
            )
        out.print(t)

    _run_async(flow, verbose=verbose, db="off")


@llm_app.command("probe")
def llm_probe(
    endpoint: Annotated[str | None, typer.Option("--endpoint", "-e", help="probe one endpoint only")] = None,
    tier: Annotated[str, typer.Option("--tier", "-t", help="small | large | all")] = "all",
    as_json: JsonOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """逐个测试接入点的连通性和功能(会产生少量真实调用费用)/ probe endpoints (small real cost)."""
    from app.llm.probe import probe_endpoint, suggested_features

    tiers = ["small", "large"] if tier == "all" else [tier]

    async def flow(s: Services) -> None:
        eps = [s.gateway.endpoint(endpoint)] if endpoint else s.gateway.config.ordered()
        all_reports = []
        for ep in eps:
            adapter = s.gateway.adapters.get(ep.adapter)
            reports = []
            for tr in tiers:
                if adapter is None:
                    continue
                rep = await probe_endpoint(adapter, ep, tr)  # type: ignore[arg-type]
                reports.append(rep)
            all_reports.append((ep, reports))
        if as_json:
            out.print_json(
                json.dumps(
                    [
                        {
                            "endpoint": ep.name,
                            "reports": [r.model_dump() for r in reps],
                            "suggested": suggested_features(reps),
                        }
                        for ep, reps in all_reports
                    ],
                    ensure_ascii=False,
                )
            )
            return
        for ep, reps in all_reports:
            t = Table("tier", "model", "check", "result", "ms", "detail", title=ep.name)
            for r in reps:
                for c in r.checks:
                    color = {"ok": "green", "fail": "red", "skipped": "yellow"}[c.status]
                    t.add_row(
                        r.tier,
                        r.model or "-",
                        c.name,
                        f"[{color}]{c.status}[/{color}]",
                        str(c.latency_ms),
                        c.detail[:80],
                    )
            out.print(t)
            sug = suggested_features(reps)
            if sug:
                out.print(f"  suggested features for [bold]{ep.name}[/bold]: {json.dumps(sug)}\n")

    _run_async(flow, verbose=verbose, db="off")


# --- dl db --------------------------------------------------------------------


@db_app.command("init")
def db_init(verbose: VerboseOpt = False) -> None:
    """创建索引 / create indexes."""
    from app.db.client import Mongo
    from app.db.indexes import ensure_indexes

    settings = _settings(verbose)

    async def main() -> None:
        mongo = Mongo(settings)
        try:
            if not await mongo.ping():
                err.print(f"[red]MongoDB not reachable: {settings.mongo_uri}[/red]")
                raise typer.Exit(1)
            await ensure_indexes(mongo.db, settings.activity_cache_ttl_days)
            out.print(f"[green]indexes ok[/green] ({settings.mongo_db})")
        finally:
            await mongo.close()

    asyncio.run(main())


# M2 commands (org, team, report, consent, token, worker) register themselves on `app`.
from app.cli import m3_cmds, m5_cmds, m6_cmds, m7_cmds, org_cmds  # noqa: E402, F401


def main() -> None:
    app()


if __name__ == "__main__":
    main()
