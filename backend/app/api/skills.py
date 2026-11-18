"""Skill endpoints: the contract every entry point (Web, CLI, DingTalk, VSCode) builds on."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.auth import Caller, caller, user_caller
from app.skills.manifest import Skill
from app.skills.runner import SkillRunResult

router = APIRouter(prefix="/skills", tags=["skills"])


class SkillInfo(BaseModel):
    name: str
    version: str
    description: str
    layer: str
    roles: list[str]
    model: str
    langs: list[str]
    commands: list[str]
    intents: list[str]
    inputs: list[dict[str, Any]]
    render: list[str]
    template: str


class RunRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    lang: str | None = None


def _info(s: Skill) -> SkillInfo:
    m = s.manifest
    return SkillInfo(
        name=m.name,
        version=m.version,
        description=m.description,
        layer=s.layer,
        roles=list(m.roles),
        model=m.model,
        langs=s.langs,
        commands=m.triggers.commands,
        intents=m.triggers.intents,
        inputs=[i.model_dump(exclude={"untrusted"}) for i in m.inputs],
        render=list(m.output.render),
        template=m.output.template,
    )


@router.get("", response_model=list[SkillInfo])
async def list_skills(request: Request, c: Caller = Depends(caller)) -> list[SkillInfo]:
    return [_info(s) for s in request.app.state.services.registry.list()]


@router.get("/{name}", response_model=SkillInfo)
async def get_skill(name: str, request: Request, c: Caller = Depends(caller)) -> SkillInfo:
    return _info(request.app.state.services.registry.get(name))


@router.post("/{name}/run", response_model=SkillRunResult)
async def run_skill(
    name: str, body: RunRequest, request: Request, c: Caller = Depends(user_caller)
) -> SkillRunResult:
    runner = request.app.state.services.runner
    return await runner.run(name, body.inputs, user_id=c.user_id, lang=body.lang, client_id=c.client_id)
