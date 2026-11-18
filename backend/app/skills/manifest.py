"""``skill.yaml`` schema and the loaded-skill object.

A skill directory holds:

* ``skill.yaml``            - this manifest
* ``prompt.<lang>.md``      - instructions per language (``prompt.zh.corp.md`` = endpoint variant)
* ``schema.py`` / ``schema.json`` - output schema (``schema.py`` only for built-in skills)
* ``examples/*.json``       - few-shot examples and eval cases
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.llm.types import DataLevel, Effort, Tier

Layer = Literal["builtin", "team", "user"]
InputType = Literal["text", "string", "date", "date_range", "enum", "int", "bool", "json"]
RenderTarget = Literal["markdown", "dingtalk_card", "json"]

_PROMPT_RE = re.compile(r"^prompt\.(zh|en)(?:\.([a-z0-9_-]+))?\.md$")


class ManifestError(ValueError):
    pass


class InputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: InputType = "text"
    required: bool = False
    description: str = ""
    default: Any = None
    choices: list[str] | None = None
    max_chars: int | None = None
    # Free text is wrapped as untrusted by default; set false only for values we control.
    untrusted: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> InputSpec:
        if self.type == "enum" and not self.choices:
            raise ValueError(f"input '{self.name}': enum needs choices")
        if self.untrusted is None:
            self.untrusted = self.type in ("text", "string", "json")
        return self


class Triggers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commands: list[str] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)


class OutputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_ref: str | None = Field(default=None, alias="schema")
    render: list[RenderTarget] = Field(default_factory=lambda: ["markdown"])
    # Markdown renderer: daily_report | review | manager_review | team_rollup | generic
    template: str = "generic"


class ContextSpec(BaseModel):
    """A context provider the runner calls before the model (``app/skills/providers.py``)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    params: dict[str, Any] = Field(default_factory=dict)
    # optional: when storage, the user's org membership or consent is missing, the skill still
    # runs and the context says why the data is unavailable
    optional: bool = False


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,48}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str
    roles: list[Literal["member", "manager"]] = Field(default_factory=lambda: ["member"])
    triggers: Triggers = Field(default_factory=Triggers)
    inputs: list[InputSpec] = Field(default_factory=list)
    context: list[ContextSpec] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    model: Tier = "large"
    fallback: Literal["allow", "deny"] = "allow"
    # Remind the reader to double-check when a backup model produced the result.
    review_on_fallback: bool = False
    max_tool_turns: int = Field(default=0, ge=0, le=10)
    effort: Effort | None = None
    max_tokens: int = Field(default=16000, ge=256, le=64000)
    data_level: DataLevel = "L3"
    output: OutputSpec = Field(default_factory=OutputSpec)
    permissions: list[str] = Field(default_factory=list)

    @field_validator("permissions")
    @classmethod
    def _perms(cls, v: list[str]) -> list[str]:
        bad = [p for p in v if not re.fullmatch(r"(read|write):[a-z_]+", p)]
        if bad:
            raise ValueError(f"invalid permissions {bad}; expected 'read:<scope>' or 'write:<scope>'")
        return v

    @field_validator("context", mode="before")
    @classmethod
    def _context_shorthand(cls, v: Any) -> Any:
        # allow `context: [team_snapshot]` as well as `[{name: team_snapshot, params: {...}}]`
        return [{"name": c} if isinstance(c, str) else c for c in (v or [])]

    @model_validator(mode="after")
    def _unique_inputs(self) -> SkillManifest:
        names = [i.name for i in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError("input names must be unique")
        if self.max_tool_turns and not self.tools:
            raise ValueError("max_tool_turns > 0 requires a tools whitelist")
        return self


@dataclass
class Example:
    name: str
    lang: str
    inputs: dict[str, Any]
    output: dict[str, Any] | None = None
    fewshot: bool = False
    expect: dict[str, Any] = field(default_factory=dict)  # eval assertions (see evals/)
    # recorded context-provider data ({provider: data}) for skills with `context:`
    context: dict[str, Any] | None = None


@dataclass
class Skill:
    manifest: SkillManifest
    path: Path
    layer: Layer
    prompts: dict[str, str]  # "zh", "en", "zh.corp", ...
    output_model: type[BaseModel] | None = None
    output_schema: dict[str, Any] | None = None
    examples: list[Example] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def langs(self) -> list[str]:
        return sorted({k.split(".")[0] for k in self.prompts})

    def prompt_for(self, lang: str, variant: str | None = None) -> tuple[str, str]:
        """Return (prompt text, language of that prompt), preferring the variant, then the language."""
        for key in ([f"{lang}.{variant}"] if variant else []) + [lang]:
            if key in self.prompts:
                return self.prompts[key], lang
        other = self.langs[0]
        return self.prompts[other], other

    def validate_output(self, data: Any) -> tuple[Any, list[str]]:
        """Validate against the output schema. Returns (normalised data, errors)."""
        if self.output_model is not None:
            try:
                return self.output_model.model_validate(data).model_dump(mode="json"), []
            except ValidationError as e:
                return data, [
                    f"{'.'.join(map(str, err['loc'])) or '<root>'}: {err['msg']}" for err in e.errors()
                ]
        if self.output_schema is not None:
            v = jsonschema.Draft202012Validator(self.output_schema)
            errs = [
                f"{'.'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in v.iter_errors(data)
            ]
            return data, errs
        return data, []


# --- loading ------------------------------------------------------------------


def _load_py_schema(path: Path, attr: str, skill_name: str) -> type[BaseModel]:
    digest = hashlib.sha1(str(path).encode()).hexdigest()[:8]
    mod_name = f"dayline_skill_{skill_name.replace('-', '_')}_{digest}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ManifestError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module  # needed for pydantic forward refs
    spec.loader.exec_module(module)
    model = getattr(module, attr, None)
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise ManifestError(f"{path.name}:{attr} is not a pydantic model")
    return model


def load_skill(path: Path, layer: Layer) -> Skill:
    try:
        raw = yaml.safe_load((path / "skill.yaml").read_text(encoding="utf-8")) or {}
        manifest = SkillManifest.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as e:
        raise ManifestError(f"skill.yaml invalid: {e}") from e
    if manifest.name != path.name:
        raise ManifestError(f"skill name '{manifest.name}' must match its directory name '{path.name}'")

    prompts: dict[str, str] = {}
    for f in sorted(path.iterdir()):
        m = _PROMPT_RE.match(f.name)
        if m:
            key = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
            prompts[key] = f.read_text(encoding="utf-8").strip()
    if not any("." not in k for k in prompts):
        raise ManifestError("needs at least one prompt.zh.md or prompt.en.md")

    skill = Skill(manifest=manifest, path=path, layer=layer, prompts=prompts)

    ref = manifest.output.schema_ref
    if ref:
        file_part, _, attr = ref.partition(":")
        schema_file = (path / file_part).resolve()
        if schema_file.parent != path.resolve():
            raise ManifestError("output.schema must name a file inside the skill directory")
        if not schema_file.is_file():
            raise ManifestError(f"schema file not found: {file_part}")
        if schema_file.suffix == ".py":
            if layer != "builtin":
                # Team/user skills are mounted volumes; running their Python inside the API
                # process would hand them our secrets and DB access.
                raise ManifestError("schema.py is only allowed for built-in skills; use schema.json")
            if not attr:
                raise ManifestError("python schema needs 'schema.py:ModelName'")
            skill.output_model = _load_py_schema(schema_file, attr, manifest.name)
            skill.output_schema = skill.output_model.model_json_schema()
        elif schema_file.suffix == ".json":
            schema = json.loads(schema_file.read_text(encoding="utf-8"))
            try:
                jsonschema.Draft202012Validator.check_schema(schema)
            except jsonschema.SchemaError as e:
                raise ManifestError(f"schema.json is not a valid JSON Schema: {e.message}") from e
            skill.output_schema = schema
        else:
            raise ManifestError("output.schema must be a .py or .json file")

    ex_dir = path / "examples"
    if ex_dir.is_dir():
        for f in sorted(ex_dir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                skill.examples.append(
                    Example(
                        name=d.get("name", f.stem),
                        lang=d.get("lang", "zh"),
                        inputs=d["inputs"],
                        output=d.get("output"),
                        fewshot=bool(d.get("fewshot", False)),
                        expect=d.get("expect", {}),
                        context=d.get("context"),
                    )
                )
            except (ValueError, KeyError) as e:
                raise ManifestError(f"example {f.name} invalid: {e}") from e
            needed = {c.name for c in manifest.context if not c.optional}
            ex = skill.examples[-1]
            if needed and not needed <= set(ex.context or {}):
                raise ManifestError(f"example {f.name} needs 'context' for {sorted(needed)}")
    return skill


def coerce_inputs(specs: list[InputSpec], raw: dict[str, Any], *, today: date) -> dict[str, Any]:
    """Validate and normalise run inputs against the manifest. Raises ``ValueError``."""
    known = {s.name for s in specs}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"unknown inputs: {unknown}")
    out: dict[str, Any] = {}
    for s in specs:
        v = raw.get(s.name)
        if v is None or v == "":
            if s.default is not None:
                v = s.default
            elif s.type == "date" and s.required:
                v = "today"
            elif s.required:
                raise ValueError(f"missing required input '{s.name}'")
            else:
                continue
        if s.type in ("text", "string"):
            v = str(v)
            if s.max_chars and len(v) > s.max_chars:
                raise ValueError(f"input '{s.name}' exceeds {s.max_chars} characters")
        elif s.type == "date":
            v = today.isoformat() if v == "today" else date.fromisoformat(str(v)).isoformat()
        elif s.type == "date_range":
            a, _, b = str(v).partition("..")
            a_d, b_d = date.fromisoformat(a.strip()), date.fromisoformat((b or a).strip())
            if b_d < a_d:
                raise ValueError(f"input '{s.name}': range end before start")
            v = f"{a_d.isoformat()}..{b_d.isoformat()}"
        elif s.type == "enum":
            if str(v) not in (s.choices or []):
                raise ValueError(f"input '{s.name}' must be one of {s.choices}")
            v = str(v)
        elif s.type == "int":
            v = int(v)
        elif s.type == "bool":
            v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "y")
        elif s.type == "json":
            v = json.loads(v) if isinstance(v, str) else v
        out[s.name] = v
    return out
