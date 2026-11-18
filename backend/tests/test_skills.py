from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from app.rules.engine import RuleContext, RuleEngine
from app.skills.manifest import coerce_inputs
from app.skills.registry import SkillRegistry
from app.skills.router import SkillRouter
from app.tools.registry import ToolRegistry
from tests.conftest import SKILLS

TOOLS = ToolRegistry.with_local_tools().permissions()
RULES = RuleEngine().names()


def registry(root: Path, **kw) -> SkillRegistry:
    return SkillRegistry(root, known_tools=TOOLS, known_rules=RULES, **kw)


def write_skill(
    root: Path, layer: str, name: str, manifest: str, *, prompt: str = "做事", files: dict | None = None
):
    d = root / layer / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "skill.yaml").write_text(manifest, encoding="utf-8")
    (d / "prompt.zh.md").write_text(prompt, encoding="utf-8")
    for fname, content in (files or {}).items():
        (d / fname).write_text(content, encoding="utf-8")
    return d


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copytree(SKILLS / "builtin", tmp_path / "builtin")
    return tmp_path


def test_builtin_skills_load_cleanly():
    reg = registry(SKILLS)
    assert reg.problems == []
    names = {s.name for s in reg.list()}
    assert {"generate-daily", "review-member", "weekly-rollup", "daily-to-english"} <= names
    assert reg.get("weekly-rollup").layer == "team"
    assert reg.get("daily-to-english").output_schema is not None
    gd = reg.get("generate-daily")
    assert gd.layer == "builtin" and gd.langs == ["en", "zh"]
    assert gd.output_model is not None and gd.output_schema["title"] == "DailyReport"


def test_fewshot_examples_pass_their_own_schema_and_rules():
    from app.skills.context import replay

    reg = registry(SKILLS)
    engine = RuleEngine()
    for skill in reg.list():
        for ex in skill.examples:
            if ex.output is None:
                continue
            data, errors = skill.validate_output(ex.output)
            assert errors == [], (skill.name, ex.name, errors)
            ctx = RuleContext(skill=skill.name, lang=ex.lang, inputs=ex.inputs)
            if ex.context:  # same extras a real run derives from the context providers
                results = {name: replay(name, d) for name, d in ex.context.items()}
                quotes = [r.quote_source for r in results.values() if r.quote_source]
                ctx.extras.update(
                    quote_source="\n".join(quotes) or None,
                    context_meta={n: r.meta for n, r in results.items()},
                )
            findings = [f for f in engine.run(skill.manifest.rules, "post", data, ctx) if f.level == "error"]
            assert findings == [], (skill.name, ex.name, findings)


KNOWN_EXPECT_KEYS = {
    "status",
    "min_items",
    "questions",
    "max_questions",
    "contains",
    "not_contains",
    "all_sources",
    "rating",
    "min_trend_flags",
    "max_trend_flags",
    "min_escalations",
    "max_escalations",
    "members_exclude",
    "not_in_sections",
    "stats",
    "min_issues",
    "max_issues",
    "min_consistency_notes",
    "max_consistency_notes",
}


def test_example_expectations_use_known_keys():
    for skill in registry(SKILLS).list():
        for ex in skill.examples:
            assert set(ex.expect) <= KNOWN_EXPECT_KEYS, (
                skill.name,
                ex.name,
                set(ex.expect) - KNOWN_EXPECT_KEYS,
            )


def test_every_builtin_skill_has_examples_in_both_languages():
    for skill in registry(SKILLS).list():
        assert {e.lang for e in skill.examples} == {"zh", "en"}, skill.name
        assert any(e.fewshot for e in skill.examples), skill.name


async def test_reference_outputs_meet_their_expectations_through_the_runner():
    """Each few-shot output, returned by a scripted model, goes through the real runner
    (context replay, schema, rules, overlay, rendering) and must satisfy its own `expect`."""
    import json

    from app.skills.runner import SkillRunner
    from evals.checks import check
    from tests.conftest import FakeAdapter, make_gateway, resp

    reg = registry(SKILLS, hot_reload=False)
    for skill in reg.list():
        for ex in [e for e in skill.examples if e.output is not None]:
            a = FakeAdapter({"e1": [resp(json.dumps(ex.output, ensure_ascii=False))]})
            runner = SkillRunner(
                registry=reg,
                gateway=make_gateway(a),
                tools=ToolRegistry.with_local_tools(),
                rules=RuleEngine(),
            )
            r = await runner.run(
                skill.name, ex.inputs, user_id="eval", lang=ex.lang, context_override=ex.context
            )
            assert check(ex.expect, r) == [], (skill.name, ex.name, check(ex.expect, r), r.errors)
            if ex.context:
                prompt = a.calls[0][2].messages[0].text
                assert all(f'name="{n}"' in prompt for n in ex.context)  # context reached the model


MINIMAL = """\
name: weekly-rollup
version: 1.0.0
description: 周报
inputs:
  - {name: notes, type: text, required: true}
output:
  schema: schema.json
"""


def test_new_team_skill_needs_no_python_and_hot_reloads(root: Path):
    reg = registry(root, poll_interval_s=0)
    assert "weekly-rollup" not in {s.name for s in reg.list()}
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}
    write_skill(root, "team", "weekly-rollup", MINIMAL, files={"schema.json": json.dumps(schema)})
    s = reg.get("weekly-rollup")  # picked up on the next access
    assert s.layer == "team" and s.output_schema == schema
    assert s.validate_output({"summary": "ok"})[1] == []
    assert s.validate_output({})[1]


def test_python_schema_rejected_outside_builtin(root: Path):
    write_skill(
        root,
        "team",
        "weekly-rollup",
        MINIMAL.replace("schema.json", "schema.py:Out"),
        files={"schema.py": "import os\nfrom pydantic import BaseModel\nclass Out(BaseModel):\n    a: str\n"},
    )
    reg = registry(root)
    assert "weekly-rollup" not in {s.name for s in reg.list()}
    assert "only allowed for built-in" in reg.problems[0].error


@pytest.mark.parametrize(
    ("manifest", "error"),
    [
        (MINIMAL.replace("name: weekly-rollup", "name: other-name"), "must match its directory"),
        (MINIMAL + "tools: [no_such_tool]\n", "unknown tools"),
        (MINIMAL + "rules: [no-such-rule]\n", "unknown rules"),
        (MINIMAL + "colour: blue\n", "Extra inputs are not permitted"),
        (MINIMAL.replace("1.0.0", "v1"), "version"),
        (MINIMAL + "permissions: [everything]\n", "invalid permissions"),
        (MINIMAL + "max_tool_turns: 2\n", "requires a tools whitelist"),
        (MINIMAL + "context: [no_such_provider]\n", "unknown context providers"),
        (MINIMAL + "context: [team_snapshot]\n", "needs permission 'read:team_reports'"),
        (MINIMAL + "tools: [get_recent_reports]\n", "needs permission 'read:own_reports'"),
    ],
)
def test_invalid_manifests_are_reported_not_loaded(root: Path, manifest: str, error: str):
    write_skill(root, "team", "weekly-rollup", manifest, files={"schema.json": "{}"})
    reg = registry(root)
    assert "weekly-rollup" not in {s.name for s in reg.list()}
    assert error in reg.problems[0].error


def test_team_skill_cannot_shadow_builtin(root: Path):
    m = MINIMAL.replace("weekly-rollup", "review-member")
    write_skill(root, "team", "review-member", m, files={"schema.json": "{}"})
    reg = registry(root)
    assert reg.get("review-member").layer == "builtin"
    assert "already used by the builtin skill" in reg.problems[0].error


def test_router_matches_explicit_commands():
    router = SkillRouter(registry(SKILLS))
    r = router.route("/gen 今天修了三个 bug")
    assert r and r.skill.name == "generate-daily" and r.rest == "今天修了三个 bug"
    assert router.route("dl review").skill.name == "review-member"
    assert router.route("/generate") is None
    assert router.route("帮我写日报") is None  # intent routing arrives in M3


def test_coerce_inputs():
    reg = registry(SKILLS)
    specs = reg.get("generate-daily").manifest.inputs
    today = date(2026, 11, 18)
    v = coerce_inputs(specs, {"narrative": "x"}, today=today)
    assert v == {"date": "2026-11-18", "narrative": "x"}
    with pytest.raises(ValueError, match="missing required"):
        coerce_inputs(specs, {}, today=today)
    with pytest.raises(ValueError, match="unknown inputs"):
        coerce_inputs(specs, {"narrative": "x", "evil": 1}, today=today)
    with pytest.raises(ValueError):
        coerce_inputs(specs, {"narrative": "x", "date": "yesterday"}, today=today)
    with pytest.raises(ValueError, match="exceeds"):
        coerce_inputs(specs, {"narrative": "x" * 20001}, today=today)
