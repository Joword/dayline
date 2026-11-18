"""Org-aware endpoints: the caller, their reports, their team (M2)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import Caller, caller, user_caller
from app.bootstrap import Services
from app.core.security import hash_token, new_token
from app.db.models import ApiToken
from app.integrations.models import CalendarConfig, GitConfig
from app.relay.views import ReportView, TeamSnapshot
from app.skills.runner import SkillRunResult

router = APIRouter(tags=["org"])


def org_services(request: Request) -> Services:
    s: Services = request.app.state.services
    if s.relay is None or s.report_service is None:
        raise HTTPException(503, "report storage is not available")
    return s


async def _viewer(s: Services, c: Caller):
    return await s.relay.viewer(c.user_id, client_id=c.client_id)  # type: ignore[union-attr,arg-type]


# --- me -----------------------------------------------------------------------------


@router.get("/me")
async def me(c: Caller = Depends(user_caller), s: Services = Depends(org_services)) -> dict[str, Any]:
    v = await _viewer(s, c)
    u = await s.relay.user(c.user_id)  # type: ignore[union-attr,arg-type]
    manager = await s.users.get(u.manager_id) if u.manager_id else None  # type: ignore[union-attr]
    return {
        "user": {
            "id": u.id,
            "name": u.name,
            "org_unit": u.org_unit,
            "system_roles": u.system_roles,
            "lang": u.lang,
        },
        "manager": {"id": manager.id, "name": manager.name} if manager else None,
        "direct_reports": [b.model_dump() for b in await s.relay.direct_reports(v)],  # type: ignore[union-attr]
        "consents": [x.model_dump() for x in await s.relay.consents(v)],  # type: ignore[union-attr]
        "tokens": [
            {"id": t.id, "label": t.label, "created_at": t.created_at, "last_used_at": t.last_used_at}
            for t in u.api_tokens
        ],
    }


class ConsentBody(BaseModel):
    scope: Literal["git", "calendar"]


@router.post("/me/consents")
async def grant_consent(
    body: ConsentBody, c: Caller = Depends(user_caller), s: Services = Depends(org_services)
):
    return (await s.relay.grant_consent(await _viewer(s, c), body.scope)).model_dump()  # type: ignore[union-attr]


@router.delete("/me/consents/{scope}")
async def revoke_consent(
    scope: Literal["git", "calendar"], c: Caller = Depends(user_caller), s: Services = Depends(org_services)
):
    return {"revoked": await s.relay.revoke_consent(await _viewer(s, c), scope)}  # type: ignore[union-attr]


class TokenBody(BaseModel):
    label: str = Field(default="", max_length=80)


@router.post("/me/tokens")
async def create_token(
    body: TokenBody, c: Caller = Depends(user_caller), s: Services = Depends(org_services)
):
    await _viewer(s, c)
    token_id, token = new_token()
    await s.users.add_token(c.user_id, ApiToken(id=token_id, hash=hash_token(token), label=body.label))  # type: ignore[union-attr,arg-type]
    return {"id": token_id, "token": token, "note": "shown once; store it now"}


@router.delete("/me/tokens/{token_id}")
async def revoke_token(token_id: str, c: Caller = Depends(user_caller), s: Services = Depends(org_services)):
    return {"revoked": await s.users.revoke_token(c.user_id, token_id)}  # type: ignore[union-attr,arg-type]


# --- reports --------------------------------------------------------------------------


class GenerateBody(BaseModel):
    narrative: str
    date: str | None = None
    answers: str | None = None
    lang: str | None = None


class GenerateResponse(BaseModel):
    result: SkillRunResult
    report: dict[str, Any] | None = None


@router.post("/reports/generate", response_model=GenerateResponse)
async def generate(body: GenerateBody, c: Caller = Depends(user_caller), s: Services = Depends(org_services)):
    inputs = {"narrative": body.narrative, "date": body.date, "answers": body.answers}
    result = await s.runner.run(
        "generate-daily", inputs, user_id=c.user_id, lang=body.lang, client_id=c.client_id
    )  # type: ignore[arg-type]
    report = None
    if result.status == "ok":
        raw = {"narrative": body.narrative, "answers": body.answers}
        report = await s.report_service.save_generated(c.user_id, result, raw)  # type: ignore[union-attr,arg-type]
    return GenerateResponse(result=result, report=_public(report))


@router.post("/reports/{report_id}/confirm")
async def confirm(report_id: str, c: Caller = Depends(user_caller), s: Services = Depends(org_services)):
    return _public(await s.report_service.confirm(c.user_id, report_id))  # type: ignore[union-attr,arg-type]


@router.get("/reports/{user_id}/history", response_model=list[ReportView])
async def history(
    user_id: str,
    before: str | None = None,
    limit: int = 5,
    c: Caller = Depends(user_caller),
    s: Services = Depends(org_services),
):
    return await s.relay.history(await _viewer(s, c), user_id, before=before, limit=min(limit, 30))  # type: ignore[union-attr]


@router.get("/reports/{user_id}/{date}", response_model=ReportView)
async def get_report(
    user_id: str, date: str, c: Caller = Depends(user_caller), s: Services = Depends(org_services)
):
    return await s.relay.get_report(await _viewer(s, c), user_id, date)  # type: ignore[union-attr]


# --- team -----------------------------------------------------------------------------


@router.get("/team", response_model=TeamSnapshot)
async def team(
    date: str | None = None, c: Caller = Depends(user_caller), s: Services = Depends(org_services)
):
    day = date or s.runner.today().isoformat()
    return await s.relay.team_snapshot(await _viewer(s, c), day)  # type: ignore[union-attr]


class ReviewBody(BaseModel):
    member: str
    date: str | None = None
    tone: Literal["encouraging", "direct"] = "encouraging"
    lang: str | None = None


@router.post("/team/reviews")
async def review(body: ReviewBody, c: Caller = Depends(user_caller), s: Services = Depends(org_services)):
    result, review_id = await s.report_service.review_member(  # type: ignore[union-attr]
        c.user_id,
        body.member,
        body.date,
        tone=body.tone,
        lang=body.lang,  # type: ignore[arg-type]
    )
    return {"result": result, "review_id": review_id}


class RollupBody(BaseModel):
    date: str | None = None
    lang: str | None = None


@router.post("/team/rollup")
async def rollup(body: RollupBody, c: Caller = Depends(user_caller), s: Services = Depends(org_services)):
    result, draft, warnings = await s.report_service.draft_rollup(c.user_id, body.date, lang=body.lang)  # type: ignore[union-attr,arg-type]
    return {"result": result, "report": _public(draft), "warnings": warnings}


class ConfirmRollupBody(BaseModel):
    escalate: list[int] = Field(
        default_factory=list, description="1-based indexes of escalation candidates to send up"
    )
    forward: list[str] = Field(
        default_factory=list, description="ids of escalations waiting for you to pass further up"
    )


@router.post("/team/rollup/{report_id}/confirm")
async def confirm_rollup(
    report_id: str,
    body: ConfirmRollupBody,
    c: Caller = Depends(user_caller),
    s: Services = Depends(org_services),
):
    doc, stale = await s.report_service.confirm_rollup(  # type: ignore[union-attr]
        c.user_id,
        report_id,
        escalate=body.escalate,
        forward=body.forward,  # type: ignore[arg-type]
    )
    return {"report": _public(doc), "stale_sources": stale}


def _public(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """The author's own report as returned to them (drop internal bookkeeping)."""
    if doc is None:
        return None
    return {k: v for k, v in doc.items() if k not in ("ai_draft", "skill_run_id", "schema_version")}


# --- integrations & activity (M3) ----------------------------------------------------------


def _integration_public(d) -> dict[str, Any]:
    return {
        "id": d.id,
        "type": d.type,
        "provider": d.provider,
        "label": d.label,
        "status": d.status,
        "mode_used": d.state.get("mode_used"),
        "last_ok_at": d.last_ok_at,
        "last_error": d.last_error,
    }


@router.get("/me/integrations")
async def list_integrations(c: Caller = Depends(user_caller), s: Services = Depends(org_services)):
    return [_integration_public(d) for d in await s.integrations.for_user(c.user_id)]  # type: ignore[union-attr,arg-type]


class IntegrationBody(BaseModel):
    config: GitConfig | CalendarConfig = Field(discriminator="type")


@router.post("/me/integrations")
async def add_integration(
    body: IntegrationBody, c: Caller = Depends(user_caller), s: Services = Depends(org_services)
):
    await _viewer(s, c)
    doc = await s.activity.add(c.user_id, body.config)  # type: ignore[union-attr,arg-type]
    return _integration_public(doc)  # credentials are write-only: never returned


@router.delete("/me/integrations/{integration_id}")
async def remove_integration(
    integration_id: str, c: Caller = Depends(user_caller), s: Services = Depends(org_services)
):
    return {"removed": await s.integrations.remove(c.user_id, integration_id)}  # type: ignore[union-attr,arg-type]


@router.get("/me/activity")
async def my_activity(
    date: str | None = None,
    refresh: bool = False,
    c: Caller = Depends(user_caller),
    s: Services = Depends(org_services),
):
    day = date or s.runner.today().isoformat()
    return await s.relay.activity(await _viewer(s, c), c.user_id, day, refresh=refresh)  # type: ignore[union-attr,arg-type]


@router.get("/activity/{user_id}/{date}")
async def member_activity(
    user_id: str, date: str, c: Caller = Depends(user_caller), s: Services = Depends(org_services)
):
    """A direct report's activity, per source only with their consent."""
    return await s.relay.activity(await _viewer(s, c), user_id, date)  # type: ignore[union-attr]


class RouteBody(BaseModel):
    text: str = Field(max_length=4000)
    lang: str | None = None


@router.post("/route")
async def route(body: RouteBody, c: Caller = Depends(user_caller), s: Services = Depends(org_services)):
    is_manager = await s.relay.is_manager(c.user_id)  # type: ignore[union-attr,arg-type]
    r = await s.router.resolve(
        body.text, s.gateway, lang=body.lang or s.settings.default_lang, is_manager=is_manager
    )
    if r is None:
        return {"skill": None}
    return {"skill": r.skill.name, "via": r.via, "confidence": r.confidence, "rest": r.rest}


class IngestBody(BaseModel):
    user_id: str
    date: str
    source: Literal["git", "calendar"]
    payload: dict[str, Any]


@router.post("/internal/activity")
async def ingest_activity(body: IngestBody, c: Caller = Depends(caller), s: Services = Depends(org_services)):
    """Summaries pushed by the intranet collector: service token + X-Dayline-Client: collector."""
    if c.via != "service" or c.client_id != "collector":
        raise HTTPException(403, "collector endpoint")
    await s.relay.user(body.user_id)  # type: ignore[union-attr]
    await s.activity.ingest(body.user_id, body.date, body.source, body.payload)  # type: ignore[union-attr]
    return {"ok": True}


# --- escalations & notifications (M5) ----------------------------------------------------------


def _esc_services(s: Services) -> Services:
    if s.escalations is None or s.notifications is None:
        raise HTTPException(503, "escalations are not available")
    return s


def _esc_public(e) -> dict[str, Any]:
    return e.model_dump(mode="json")


@router.get("/escalations")
async def list_escalations(
    scope: Literal["held", "raised", "involved"] = "held",
    include_resolved: bool = False,
    c: Caller = Depends(user_caller),
    s: Services = Depends(org_services),
):
    esc = _esc_services(s).escalations
    return [_esc_public(e) for e in await esc.find_for(c.user_id, scope, include_resolved=include_resolved)]  # type: ignore[union-attr,arg-type]


class RaiseBody(BaseModel):
    member: str
    date: str
    item_index: int = Field(ge=0, description="0-based index into the report's items; must be a blocker")
    escalate: bool = True  # false = track at your level only
    note: str | None = Field(default=None, max_length=2000)


@router.post("/escalations")
async def raise_escalation(
    body: RaiseBody, c: Caller = Depends(user_caller), s: Services = Depends(org_services)
):
    esc = _esc_services(s).escalations
    doc = await esc.raise_from_report(
        c.user_id, body.member, body.date, body.item_index, escalate=body.escalate, note=body.note
    )  # type: ignore[union-attr,arg-type]
    return _esc_public(doc)


@router.get("/escalations/{esc_id}")
async def get_escalation(esc_id: str, c: Caller = Depends(user_caller), s: Services = Depends(org_services)):
    return _esc_public(await _esc_services(s).escalations.get(c.user_id, esc_id))  # type: ignore[union-attr,arg-type]


class NoteBody(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


@router.post("/escalations/{esc_id}/{action}")
async def act_on_escalation(
    esc_id: str,
    action: Literal["accept", "escalate", "resolve", "comment"],
    body: NoteBody,
    c: Caller = Depends(user_caller),
    s: Services = Depends(org_services),
):
    esc = _esc_services(s).escalations
    fn = {"accept": esc.accept, "escalate": esc.escalate, "resolve": esc.resolve, "comment": esc.comment}[
        action
    ]  # type: ignore[union-attr]
    if action in ("resolve", "comment"):
        return _esc_public(await fn(c.user_id, esc_id, body.note or ""))  # type: ignore[arg-type,operator]
    return _esc_public(await fn(c.user_id, esc_id, body.note))  # type: ignore[arg-type,operator]


@router.get("/me/notifications")
async def notifications(
    unread: bool = False, c: Caller = Depends(user_caller), s: Services = Depends(org_services)
):
    repo = _esc_services(s).notifications
    items = await repo.for_user(c.user_id, unread_only=unread)  # type: ignore[union-attr,arg-type]
    return {"unread": await repo.unread_count(c.user_id), "items": items}  # type: ignore[union-attr,arg-type]


class ReadBody(BaseModel):
    ids: list[str] | None = None  # None = all


@router.post("/me/notifications/read")
async def mark_read(body: ReadBody, c: Caller = Depends(user_caller), s: Services = Depends(org_services)):
    return {"marked": await _esc_services(s).notifications.mark_read(c.user_id, body.ids)}  # type: ignore[union-attr,arg-type]
