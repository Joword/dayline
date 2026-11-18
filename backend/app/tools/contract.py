"""Contract checks on a member tool's ``tools/list`` (plan §8.3, §8.6).

The definition (names, descriptions, schemas) is injected into prompts, so it is reviewed
like code: errors block submission; warnings are shown to the reviewing developer. The
hash of the normalised definition is what an approval locks.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import jsonschema

SEP = "__"  # model-facing name: "<package>__<tool>"
MAX_NAME = 64  # tool-name limit of the LLM APIs
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DESC_WARN, DESC_MAX = 600, 1024
DEFINITION_WARN_BYTES = 16_000
KEYS = ("name", "title", "description", "inputSchema", "outputSchema", "annotations")

# Text that tries to steer the model rather than describe the tool. Flagged for the reviewer.
SUSPICIOUS = [
    (re.compile(p, re.IGNORECASE), why)
    for p, why in [
        (
            r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier|other)",
            "asks the model to ignore instructions",
        ),
        (r"system\s+prompt|developer\s+message", "mentions the system prompt"),
        (r"</?\s*untrusted", "contains the untrusted-content tag"),
        (r"忽略|无视|之前的指令|以上(要求|指令)|系统提示", "asks the model to ignore instructions (zh)"),
        (
            r"\b(always|must|before\s+any\s+other)\b.{0,40}\b(call|use|invoke)\b",
            "pushes the model to call it",
        ),
        (r"(总是|必须|务必|优先).{0,10}(调用|使用)", "pushes the model to call it (zh)"),
        (r"\b(password|passwd|secret|api[\s_-]?key|token|credential)s?\b|密码|密钥|口令", "asks for secrets"),
        (r"https?://", "contains a URL"),
        (r"other\s+tools?|其他工具", "talks about other tools"),
    ]
]


def full_name(package: str, tool: str) -> str:
    return f"{package}{SEP}{tool}"


def is_mcp_ref(name: str) -> bool:
    return SEP in name


def normalize(tools: list[Any]) -> list[dict[str, Any]]:
    """mcp ``Tool`` models or plain dicts -> sorted, comparable dicts (camelCase as on the wire)."""
    out = []
    for t in tools:
        d = (
            t.model_dump(by_alias=True, exclude_none=True, mode="json")
            if hasattr(t, "model_dump")
            else dict(t)
        )
        out.append({k: d[k] for k in KEYS if d.get(k) is not None})
    return sorted(out, key=lambda d: d.get("name", ""))


def definition_hash(definition: list[dict[str, Any]]) -> str:
    raw = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class Finding:
    level: Literal["error", "warning"]
    tool: str | None
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.tool or '-'}: {self.message}"


def _strings(node: Any, path: str = ""):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")


def _hidden(s: str) -> list[str]:
    # zero-width, bidi controls, tags block: invisible to the reviewer, visible to the model
    return sorted({f"U+{ord(c):04X}" for c in s if unicodedata.category(c) == "Cf"})


def check_definition(package: str, definition: list[dict[str, Any]]) -> list[Finding]:
    f: list[Finding] = []
    if not definition:
        return [Finding("error", None, "tools/list returned no tools")]
    names = [d.get("name", "") for d in definition]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        f.append(Finding("error", dup, "duplicate tool name"))
    for d in definition:
        name = d.get("name", "")
        if not TOOL_NAME_RE.fullmatch(name):
            f.append(Finding("error", name or None, "name must match [A-Za-z0-9_-]{1,64}"))
        elif SEP in name:
            f.append(Finding("error", name, f"name must not contain '{SEP}'"))
        elif len(full_name(package, name)) > MAX_NAME:
            f.append(
                Finding("error", name, f"'{full_name(package, name)}' is longer than {MAX_NAME} characters")
            )
        desc = d.get("description") or ""
        if not desc.strip():
            f.append(Finding("error", name, "description is missing"))
        elif len(desc) > DESC_MAX:
            f.append(Finding("error", name, f"description is longer than {DESC_MAX} characters"))
        elif len(desc) > DESC_WARN:
            f.append(Finding("warning", name, f"description is long ({len(desc)} characters)"))
        schema = d.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            f.append(Finding("error", name, "inputSchema must be a JSON Schema with type: object"))
        else:
            try:
                jsonschema.Draft202012Validator.check_schema(schema)
            except jsonschema.SchemaError as e:
                f.append(Finding("error", name, f"inputSchema is not valid JSON Schema: {e.message}"))
        for path, s in _strings({k: v for k, v in d.items() if k != "name"}):
            if hidden := _hidden(s):
                f.append(Finding("error", name, f"{path} contains invisible characters {hidden}"))
            if path.rsplit(".", 1)[-1].startswith("$"):  # $schema / $id / $ref: JSON Schema plumbing
                continue
            for rx, why in SUSPICIOUS:
                if rx.search(s):
                    f.append(Finding("warning", name, f"{path}: {why}"))
    size = len(json.dumps(definition, ensure_ascii=False).encode())
    if size > DEFINITION_WARN_BYTES:
        f.append(Finding("warning", None, f"definition is large ({size} bytes); every call carries it"))
    return f


# --- examples --------------------------------------------------------------------------


@dataclass
class Example:
    name: str
    tool: str
    input: dict[str, Any]
    expect_error: bool = False
    contains: list[str] = field(default_factory=list)


def load_examples(tool_dir: Path) -> list[Example]:
    out = []
    ex_dir = tool_dir / "examples"
    if not ex_dir.is_dir():
        return out
    for p in sorted(ex_dir.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        exp = d.get("expect") or {}
        out.append(
            Example(
                name=d.get("name", p.stem),
                tool=d["tool"],
                input=d.get("input") or {},
                expect_error=bool(exp.get("is_error", False)),
                contains=list(exp.get("contains") or []),
            )
        )
    return out


CallFn = Callable[[str, dict[str, Any]], Awaitable[tuple[str, bool]]]


async def run_examples(
    examples: list[Example], definition: list[dict[str, Any]], call: CallFn
) -> list[Finding]:
    f: list[Finding] = []
    schemas = {d["name"]: d.get("inputSchema") or {} for d in definition}
    for ex in examples:
        if ex.tool not in schemas:
            f.append(Finding("error", ex.tool, f"example '{ex.name}': no such tool"))
            continue
        try:
            jsonschema.validate(ex.input, schemas[ex.tool])
        except jsonschema.ValidationError as e:
            f.append(
                Finding(
                    "error", ex.tool, f"example '{ex.name}': input does not match inputSchema: {e.message}"
                )
            )
            continue
        try:
            text, is_error = await call(ex.tool, ex.input)
        except Exception as e:  # a crash is a failed example, not a crashed check
            f.append(Finding("error", ex.tool, f"example '{ex.name}': call failed: {type(e).__name__}: {e}"))
            continue
        if is_error != ex.expect_error:
            f.append(
                Finding(
                    "error",
                    ex.tool,
                    f"example '{ex.name}': is_error={is_error}, expected {ex.expect_error}: {text[:200]}",
                )
            )
        for s in ex.contains:
            if s not in text:
                f.append(Finding("error", ex.tool, f"example '{ex.name}': result does not contain {s!r}"))
    return f
