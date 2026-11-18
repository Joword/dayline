"""Eval runner: every skill example (and extra cases) x every available endpoint.

    python -m evals                         # all skills, all ready endpoints
    python -m evals --skill generate-daily --endpoint anthropic-direct
    python -m evals --list                  # show the cases, no LLM calls

Each endpoint is evaluated on its own (failover disabled), so a weak backup model
shows up instead of hiding behind the primary. Runs make real, billed calls.
Results go to ``evals/results/<timestamp>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.llm.endpoints import load_llm_config
from app.llm.gateway import LLMGateway
from app.rules.engine import RuleEngine
from app.skills.manifest import Example
from app.skills.registry import SkillRegistry
from app.skills.runner import SkillRunError, SkillRunner
from app.tools.registry import ToolRegistry
from evals.checks import check

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"  # extra cases: cases/<skill>/*.json, same format as skill examples
RESULTS = HERE / "results"


def load_cases(registry: SkillRegistry, skill_filter: str | None) -> list[tuple[str, Example]]:
    cases: list[tuple[str, Example]] = []
    for s in registry.list():
        if skill_filter and s.name != skill_filter:
            continue
        cases += [(s.name, e) for e in s.examples]
        extra = CASES / s.name
        if extra.is_dir():
            for f in sorted(extra.glob("*.json")):
                d = json.loads(f.read_text(encoding="utf-8"))
                cases.append(
                    (
                        s.name,
                        Example(
                            d.get("name", f.stem),
                            d.get("lang", "zh"),
                            d["inputs"],
                            d.get("output"),
                            False,
                            d.get("expect", {}),
                            d.get("context"),
                        ),
                    )
                )
    return cases


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    setup_logging("WARNING", "text")
    config = load_llm_config(settings.llm_config_path)
    tools = ToolRegistry.with_local_tools()
    rules = RuleEngine()
    registry = SkillRegistry(
        settings.skills_path, known_tools=set(tools.names()), known_rules=rules.names(), hot_reload=False
    )
    cases = load_cases(registry, args.skill)
    if args.example:
        cases = [c for c in cases if c[1].name == args.example]
    if args.list:
        for skill, ex in cases:
            print(
                f"{skill:20} {ex.name:24} {ex.lang}  fewshot={ex.fewshot}  expect={json.dumps(ex.expect, ensure_ascii=False)}"
            )
        return 0

    endpoints = [e for e in config.ordered() if not args.endpoint or e.name == args.endpoint]
    rows: list[dict[str, Any]] = []
    for ep in endpoints:
        if reason := ep.unavailable_reason():
            print(f"skip {ep.name}: {reason}")
            continue
        single = config.model_copy(
            update={"failover": config.failover.model_copy(update={"order": [ep.name]})}
        )
        gateway = LLMGateway(single)
        runner = SkillRunner(
            registry=registry, gateway=gateway, tools=tools, rules=rules, timezone=settings.timezone
        )
        try:
            for skill, ex in cases:
                t0 = time.monotonic()
                row: dict[str, Any] = {"endpoint": ep.name, "skill": skill, "case": ex.name, "lang": ex.lang}
                try:
                    r = await runner.run(
                        skill, ex.inputs, user_id="eval", lang=ex.lang, context_override=ex.context
                    )
                    fails = check(ex.expect, r)
                    row.update(
                        status=r.status,
                        passed=not fails,
                        failures=fails,
                        attempts=r.validation_attempts,
                        cost=r.cost_usd,
                        tokens_in=r.usage.input_tokens,
                        tokens_out=r.usage.output_tokens,
                        output=r.output,
                    )
                except SkillRunError as e:
                    row.update(status=e.status, passed=False, failures=[str(e)])
                row["seconds"] = round(time.monotonic() - t0, 1)
                rows.append(row)
                mark = "PASS" if row["passed"] else "FAIL"
                print(f"{mark} {ep.name:22} {skill:18} {ex.name:22} {'; '.join(row['failures'])}")
        finally:
            await gateway.aclose()

    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    passed = sum(r["passed"] for r in rows)
    cost = sum(r.get("cost") or 0 for r in rows)
    print(f"\n{passed}/{len(rows)} passed · cost ${cost:.4f} · {path}")
    return 0 if passed == len(rows) else 1


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m evals")
    p.add_argument("--skill")
    p.add_argument("--example")
    p.add_argument("--endpoint")
    p.add_argument("--list", action="store_true", help="list cases without calling the LLM")
    sys.exit(asyncio.run(run(p.parse_args())))
