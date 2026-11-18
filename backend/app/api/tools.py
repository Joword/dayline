"""Member tools (M7): listing and review for people, and the Relay callbacks for tools.

People (personal token / service token + user):
  GET  /tools                      tools you can use (developers: ?all=true for every state)
  GET  /tools/{name}               details; owner and developers also see the definition
  POST /tools/{name}/{action}      approve | reject | enable | disable
  POST /internal/tools/submit      CI (service token + X-Dayline-Client: ci), the owner or a developer

Tools (a per-call grant from the call's ``_meta``, ``Authorization: Bearer dlt_...``):
  GET  /internal/tool/me                     who the call runs for
  GET  /internal/tool/reports                own recent reports           read:own_reports
  GET  /internal/tool/activity               own git / calendar activity  read:own_activity
  GET  /internal/tool/team                   team submission status       read:team_reports
  GET  /internal/tool/reports/{user}/{date}  a direct report's report     read:team_reports

Tool reads go through the Relay with client id ``tool:<name>`` and are audited, own data included.
The gateway does not route ``/v1/internal/tool/*``: tools reach the API on the `tools` network.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import Caller, caller, user_caller
from app.bootstrap import Services
from app.db.models import AuditDoc, ToolDoc
from app.relay.policy import Viewer
from app.relay.views import ReportView, TeamSnapshot
from app.tools.manifest import ManifestError, parse_manifest
from app.tools.service import CI_ACTOR, ToolError, current_approvals
from app.tools.tokens import Grant, GrantError, verify_grant

router = APIRouter(tags=["tools"])


def tool_services(request: Request) -> Services:
    s: Services = request.app.state.services
    if s.tool_service is None or s.users is None:
        raise HTTPException(503, "tool storage is not available")
    return s


def _summary(d: ToolDoc) -> dict[str, Any]:
    return {
        "name": d.name,
        "owner": d.owner,
        "version": d.version,
        "description": d.description,
        "visibility": d.visibility,
        "tools": [f"{d.name}__{t['name']}" for t in d.definition],
        "status": d.status,
    }


def _detail(d: ToolDoc) -> dict[str, Any]:
    return {
        **d.model_dump(mode="json", exclude={"history"}),
        "approvals_current": len(current_approvals(d)),
        "approvals_needed": 2 if d.manager_data else 1,
        "history": [e.model_dump(mode="json") for e in d.history[-20:]],
    }


async def _is_developer(s: Services, user_id: str) -> bool:
    u = await s.users.get(user_id)  # type: ignore[union-attr]
    return u is not None and "developer" in u.system_roles


@router.get("/tools")
async def list_tools(
    all: bool = False, c: Caller = Depends(user_caller), s: Services = Depends(tool_services)
):
    svc = s.tool_service
    if all:
        if not await _is_developer(s, c.user_id):  # type: ignore[arg-type]
            raise HTTPException(403, "only developers can list tools in every state")
        return [_summary(d) for d in await svc.repo.list()]  # type: ignore[union-attr]
    return [_summary(d) for d in await svc.usable_by(c.user_id)]  # type: ignore[union-attr,arg-type]


@router.get("/tools/{name}")
async def get_tool(name: str, c: Caller = Depends(user_caller), s: Services = Depends(tool_services)):
    svc = s.tool_service
    doc = await svc.get(name)  # type: ignore[union-attr]
    if c.user_id == doc.owner or await _is_developer(s, c.user_id):  # type: ignore[arg-type]
        return _detail(doc)
    if doc.status != "enabled" or await svc.unavailable_reason(doc, c.user_id):  # type: ignore[union-attr,arg-type]
        raise HTTPException(404, f"no tool named '{name}'")
    return _summary(doc)


class ActionBody(BaseModel):
    note: str = Field(default="", max_length=2000)  # reject / disable reason
    ack_manager_data: bool = False


@router.post("/tools/{name}/{action}")
async def act_on_tool(
    name: str,
    action: Literal["approve", "reject", "enable", "disable"],
    body: ActionBody,
    c: Caller = Depends(user_caller),
    s: Services = Depends(tool_services),
):
    svc, uid = s.tool_service, c.user_id
    if action == "approve":
        doc = await svc.approve(uid, name, ack_manager_data=body.ack_manager_data)  # type: ignore[union-attr,arg-type]
    elif action == "reject":
        doc = await svc.reject(uid, name, body.note)  # type: ignore[union-attr,arg-type]
    elif action == "enable":
        doc = await svc.enable(uid, name)  # type: ignore[union-attr,arg-type]
    else:
        doc = await svc.disable(uid, name, body.note)  # type: ignore[union-attr,arg-type]
    return _detail(doc)


class SubmitBody(BaseModel):
    manifest: dict[str, Any]  # tool.yaml as a mapping
    definition: list[dict[str, Any]]  # tools/list captured from the built image
    image_digest: str | None = None


@router.post("/internal/tools/submit")
async def submit_tool(body: SubmitBody, c: Caller = Depends(caller), s: Services = Depends(tool_services)):
    if c.via == "service" and c.client_id == "ci" and not c.user_id:
        actor = CI_ACTOR
    elif c.user_id:
        actor = c.user_id
    else:
        raise HTTPException(403, "submit as CI (X-Dayline-Client: ci) or as a user")
    try:
        manifest = parse_manifest(body.manifest)
    except ManifestError as e:
        raise ToolError(str(e)) from e
    doc, warnings, changed = await s.tool_service.submit(  # type: ignore[union-attr]
        actor, manifest, definition=body.definition, image_digest=body.image_digest
    )
    return {
        "tool": _detail(doc),
        "changed": changed,
        "warnings": [w.__dict__ for w in warnings],
    }


# --- callbacks for tools ---------------------------------------------------------------------


async def tool_grant(request: Request) -> Grant:
    s: Services = request.app.state.services
    secret = s.settings.tools_secret
    if secret is None or s.tool_service is None or s.relay is None:
        raise HTTPException(503, "member tools are off")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    try:
        grant = verify_grant(secret.get_secret_value(), token)
    except GrantError as e:
        raise HTTPException(401, f"invalid tool grant: {e}", headers={"WWW-Authenticate": "Bearer"}) from e
    doc = await s.tool_service.repo.get(grant.tool)
    if doc is None or doc.status != "enabled":
        raise HTTPException(403, "the tool is not enabled")
    return grant


async def _viewer(s: Services, g: Grant, scope: str, what: str) -> Viewer:
    """Check the grant's scope and audit the read (own data too: a tool is a third party)."""
    allowed = scope in g.scopes
    if s.audit is not None:
        await s.audit.write(
            AuditDoc(
                actor_id=g.user_id,
                client_id=f"tool:{g.tool}",
                action="tool.read",
                target=f"user:{g.user_id}/{what}",
                allowed=allowed,
                reason=None if allowed else f"grant lacks {scope}",
            )
        )
    if not allowed:
        raise HTTPException(403, f"the tool's grant does not include {scope}")
    relay_scopes = {"read:reports"} if "reports" in scope else {"read:activity"}
    return await s.relay.viewer(g.user_id, client_id=f"tool:{g.tool}", scopes=frozenset(relay_scopes))  # type: ignore[union-attr]


@router.get("/internal/tool/me")
async def tool_me(g: Grant = Depends(tool_grant), s: Services = Depends(tool_services)):
    u = await s.relay.user(g.user_id)  # type: ignore[union-attr]
    return {"user": {"id": u.id, "name": u.name, "lang": u.lang}, "tool": g.tool, "scopes": sorted(g.scopes)}


@router.get("/internal/tool/reports", response_model=list[ReportView])
async def tool_reports(
    before: str | None = None,
    limit: int = 5,
    g: Grant = Depends(tool_grant),
    s: Services = Depends(tool_services),
):
    v = await _viewer(s, g, "read:own_reports", "reports")
    views = await s.relay.history(v, g.user_id, before=before, limit=min(max(limit, 1), 30))  # type: ignore[union-attr]
    for view in views:
        view.raw_input = None  # the narrative stays with Dayline
    return views


@router.get("/internal/tool/activity")
async def tool_activity(date: str, g: Grant = Depends(tool_grant), s: Services = Depends(tool_services)):
    v = await _viewer(s, g, "read:own_activity", f"activity:{date}")
    return await s.relay.activity(v, g.user_id, date)  # type: ignore[union-attr]


@router.get("/internal/tool/team", response_model=TeamSnapshot)
async def tool_team(date: str, g: Grant = Depends(tool_grant), s: Services = Depends(tool_services)):
    v = await _viewer(s, g, "read:team_reports", f"team:{date}")
    snap = await s.relay.team_snapshot(v, date)  # type: ignore[union-attr]
    for m in snap.members:
        if m.report is not None:
            m.report.raw_input = None
    return snap


@router.get("/internal/tool/reports/{user_id}/{date}", response_model=ReportView)
async def tool_member_report(
    user_id: str, date: str, g: Grant = Depends(tool_grant), s: Services = Depends(tool_services)
):
    v = await _viewer(s, g, "read:team_reports", f"report:{user_id}:{date}")
    if user_id == g.user_id:
        raise HTTPException(422, "use /internal/tool/reports for the user's own reports")
    view = await s.relay.get_report(v, user_id, date)  # type: ignore[union-attr]
    view.raw_input = None
    return view
