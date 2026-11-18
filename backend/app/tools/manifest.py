"""``tool.yaml``: the manifest of a member MCP tool (plan §8.2).

```yaml
name: jira-sprint-summary
owner: zhangsan
version: 1.2.0
language: python
description: 汇总当前 sprint 的任务状态
image: registry.internal/tools/jira-sprint      # CI adds the digest (or write image@sha256:...)
permissions: [read:own_reports]
network_egress: [jira.company.com]
visibility: team                                  # private | team | level | company
manager_data: false                               # true needs a second approval
timeout_s: 20
resources: {cpus: 0.5, memory: 256m}
```
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.db.models import USER_ID_PATTERN

# Data a tool may read through its per-call Relay grant. Team data is manager data.
TOOL_SCOPES = {"read:own_reports", "read:own_activity", "read:team_reports"}
MANAGER_SCOPES = {"read:team_reports"}

NAME_RE = r"^[a-z][a-z0-9-]{1,40}$"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HOST_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:\d{1,5})?$")
_MEM_RE = re.compile(r"^\d+[kmg]$")


class ManifestError(ValueError):
    pass


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpus: float = Field(default=0.5, gt=0, le=2)
    memory: str = "256m"
    pids: int = Field(default=128, ge=16, le=1024)

    @field_validator("memory")
    @classmethod
    def _mem(cls, v: str) -> str:
        if not _MEM_RE.fullmatch(v.lower()):
            raise ValueError("memory must look like 256m or 1g")
        return v.lower()


class ToolManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_RE)
    owner: str = Field(pattern=USER_ID_PATTERN)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    language: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=500)
    image: str = Field(min_length=1, max_length=300)
    endpoint: str | None = None  # default http://tool-<name>:8080/mcp
    permissions: list[str] = Field(default_factory=list)
    network_egress: list[str] = Field(default_factory=list)
    visibility: Literal["private", "team", "level", "company"] = "private"
    manager_data: bool = False
    timeout_s: float = Field(default=20.0, gt=0, le=60)
    resources: Resources = Field(default_factory=Resources)

    @field_validator("permissions")
    @classmethod
    def _perms(cls, v: list[str]) -> list[str]:
        if bad := sorted(set(v) - TOOL_SCOPES):
            raise ValueError(f"unknown permissions {bad}; allowed: {sorted(TOOL_SCOPES)}")
        return sorted(set(v))

    @field_validator("network_egress")
    @classmethod
    def _egress(cls, v: list[str]) -> list[str]:
        out = []
        for h in v:
            h = h.strip().lower()
            if not _HOST_RE.fullmatch(h):
                raise ValueError(f"network_egress entry '{h}' must be a host name, '*.domain' or host:port")
            out.append(h)
        return sorted(set(out))

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"https?://[^\s/]+(/\S*)?", v):
            raise ValueError("endpoint must be an http(s) URL")
        return v

    @model_validator(mode="after")
    def _check(self) -> ToolManifest:
        if set(self.permissions) & MANAGER_SCOPES and not self.manager_data:
            raise ValueError(f"{sorted(MANAGER_SCOPES)} is manager data: set manager_data: true")
        return self

    @property
    def image_name(self) -> str:
        return self.image.split("@", 1)[0]

    @property
    def image_digest(self) -> str | None:
        _, _, digest = self.image.partition("@")
        return digest or None

    def resolved_endpoint(self) -> str:
        return self.endpoint or f"http://tool-{self.name}:8080/mcp"


def parse_manifest(raw: dict[str, Any]) -> ToolManifest:
    try:
        m = ToolManifest.model_validate(raw)
    except ValidationError as e:
        raise ManifestError(f"tool.yaml invalid: {e}") from e
    if m.image_digest is not None and not DIGEST_RE.fullmatch(m.image_digest):
        raise ManifestError("image digest must be sha256:<64 hex>")
    return m


def load_manifest(path: Path) -> ToolManifest:
    """``path`` is a tool.yaml file or the directory holding it."""
    f = path / "tool.yaml" if path.is_dir() else path
    try:
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ManifestError(f"tool.yaml is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ManifestError("tool.yaml must be a mapping")
    return parse_manifest(raw)
