"""Document models (pydantic) for the collections the backend writes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

USER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
ReportStatus = Literal["draft", "confirmed", "sent"]
SUBMITTED: tuple[str, ...] = ("confirmed", "sent")  # visible to the direct manager


def utcnow() -> datetime:
    return datetime.now(UTC)


# --- users / org --------------------------------------------------------------


class GitIdentity(BaseModel):
    provider: Literal["github", "gitlab"]
    username: str | None = None
    emails: list[str] = Field(default_factory=list)


class Consent(BaseModel):
    """Permission for the *current* direct manager to use one data source for cross-checking.

    Bound to ``grantee_id``: when the manager changes, the consent no longer applies.
    """

    scope: Literal["git", "calendar"]
    grantee_id: str
    granted_at: datetime = Field(default_factory=utcnow)


class ApiToken(BaseModel):
    id: str
    hash: str  # sha256 of the token; the token itself is shown once and never stored
    label: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    last_used_at: datetime | None = None


class UserDoc(BaseModel):
    schema_version: int = 1
    id: str = Field(pattern=USER_ID_PATTERN)  # stored as _id
    name: str
    email: str | None = None
    manager_id: str | None = None
    level: int = 0  # depth in the org tree; root = 0
    org_unit: str | None = None
    org_path: str  # materialised path "/root/.../self/" for subtree prefix queries
    system_roles: list[Literal["developer", "admin"]] = Field(default_factory=list)
    dingtalk_id: str | None = None
    git_identities: list[GitIdentity] = Field(default_factory=list)
    timezone: str | None = None
    lang: Literal["zh", "en"] | None = None
    active: bool = True
    consents: list[Consent] = Field(default_factory=list)
    api_tokens: list[ApiToken] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# --- reports ------------------------------------------------------------------


class SkillRef(BaseModel):
    name: str
    version: str


class SyncState(BaseModel):
    dingtalk_log_id: str | None = None
    status: Literal["none", "pending", "synced", "failed"] = "none"  # DingTalk log sync (M6)
    last_error: str | None = None


class ReportDoc(BaseModel):
    schema_version: int = 1
    user_id: str
    date: str  # YYYY-MM-DD in the user's timezone
    kind: Literal["individual", "manager"] = "individual"
    version: int
    status: ReportStatus = "draft"
    lang: str = "zh"
    raw_input: dict[str, Any] = Field(default_factory=dict)
    own_items: list[dict[str, Any]] = Field(default_factory=list)
    team_summary: dict[str, Any] | None = None
    skill: SkillRef
    skill_run_id: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    rendered_md: str = ""
    ai_draft: dict[str, Any] | None = None  # model output as generated; kept to diff against the final
    confirmed_at: datetime | None = None
    sync: SyncState = Field(default_factory=SyncState)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ReviewDoc(BaseModel):
    schema_version: int = 1
    report_id: str | None  # None when the reviewed text was pasted in
    report_version: int | None = None
    subject_id: str | None = None  # whose report
    reviewer_id: str
    skill: str
    rating: str | None = None
    issues: list[dict[str, Any]] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    feedback_draft: str = ""
    output: dict[str, Any] = Field(default_factory=dict)
    skill_run_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class AuditDoc(BaseModel):
    actor_id: str
    client_id: str = "dayline"
    action: str  # e.g. "report.read", "team.read", "consent.grant"
    target: str  # e.g. "user:zhangsan/report:2026-11-17"
    allowed: bool = True
    access: str | None = None
    reason: str | None = None
    ts: datetime = Field(default_factory=utcnow)


class SkillRunDoc(BaseModel):
    schema_version: int = 1
    run_id: str
    skill: str
    version: str
    user_id: str
    lang: str
    endpoint: str | None = None
    endpoints: list[str] = Field(default_factory=list)
    model: str | None = None
    failover: bool = False
    degraded: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float | None = None
    latency_ms: int = 0
    status: Literal["ok", "invalid", "refused", "error"]
    error: str | None = None
    validation_attempts: int = 1
    warnings: list[str] = Field(default_factory=list)
    trace: Any = None
    trace_truncated: bool = False
    created_at: datetime = Field(default_factory=utcnow)


# --- escalations (M5, plan §4.4) ---------------------------------------------------------

EscalationStatus = Literal["open", "escalated", "in_progress", "resolved"]


class EscalationEvent(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    actor_id: str
    action: Literal["created", "escalated", "accepted", "resolved", "commented"]
    from_holder: str | None = None
    to_holder: str | None = None
    note: str | None = None


class EscalationOrigin(BaseModel):
    user_id: str  # whose report the item came from (may be the raising manager)
    date: str
    report_id: str | None = None
    report_version: int | None = None


class EscalationDoc(BaseModel):
    schema_version: int = 1
    id: str  # stored as _id
    text: str
    need_from: str | None = None
    severity: Literal["low", "medium", "high"] | None = None
    origin: EscalationOrigin
    created_by: str
    current_holder_id: str
    status: EscalationStatus
    participants: list[str] = Field(default_factory=list)  # everyone who may see it
    chain: list[EscalationEvent] = Field(default_factory=list)
    resolution: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# --- notifications (M5; DingTalk delivery via the outbox in M6) ----------------------------


class NotificationDoc(BaseModel):
    user_id: str
    kind: str  # report.missing | rollup.ready | report.changed | escalation.*
    title: str
    body: str = ""
    link: str | None = None  # web path, e.g. /team?date=2026-11-17
    dedupe_key: str | None = None  # at most one notification per key
    created_at: datetime = Field(default_factory=utcnow)
    read_at: datetime | None = None


# --- member tools (M7, plan §8) --------------------------------------------------------------

ToolStatus = Literal["pending", "approved", "enabled", "disabled", "rejected"]
ToolVisibility = Literal["private", "team", "level", "company"]


class ToolApproval(BaseModel):
    by: str
    at: datetime = Field(default_factory=utcnow)
    definition_hash: str  # what was approved; a re-submission voids earlier approvals
    manager_data_ack: bool = False


class ToolEvent(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    actor_id: str  # user id, or "system" for automatic checks
    action: Literal["submitted", "approved", "rejected", "enabled", "disabled", "definition_changed"]
    note: str | None = None


class ToolHealth(BaseModel):
    ok: bool
    checked_at: datetime = Field(default_factory=utcnow)
    error: str | None = None


class ToolDoc(BaseModel):
    """One member MCP server (a tool package; it may expose several tools)."""

    schema_version: int = 1
    name: str  # stored as _id; the model sees each tool as "<name>__<tool>"
    owner: str
    version: str
    language: str
    description: str = ""
    image: str
    image_digest: str | None = None  # sha256:...; the deployed container is pinned to it
    endpoint: str  # streamable HTTP URL on the `tools` network
    permissions: list[str] = Field(default_factory=list)
    egress: list[str] = Field(default_factory=list)
    visibility: ToolVisibility = "private"
    manager_data: bool = False
    timeout_s: float = 20.0
    resources: dict[str, Any] = Field(default_factory=dict)
    status: ToolStatus = "pending"
    # tools/list as submitted for review; the model is only ever shown this approved text
    definition: list[dict[str, Any]] = Field(default_factory=list)
    definition_hash: str
    approvals: list[ToolApproval] = Field(default_factory=list)
    disabled_reason: str | None = None
    health: ToolHealth | None = None
    history: list[ToolEvent] = Field(default_factory=list)
    submitted_by: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# --- allocations (reserved for resource-advisor, plan §4.6 - schema only) -------------------


class AllocationDoc(BaseModel):
    """A staffing decision made by a manager after a resource-advisor suggestion.

    Reserved: no code writes this yet. Principles already fixed: metrics are computed by
    code; the model only suggests at team / task level with its evidence; a person makes
    the decision; the outcome is carried into the next day's plan and tracked."""

    schema_version: int = 1
    manager_id: str
    date: str
    task: str
    from_user_id: str | None = None
    to_user_id: str | None = None
    rationale: str = ""
    suggested_by_run: str | None = None  # skill_runs.run_id of the advice, if any
    status: Literal["proposed", "decided", "done", "dropped"] = "proposed"
    follow_up: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
