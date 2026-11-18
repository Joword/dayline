from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    s = request.app.state.services
    mongo_ok = s.mongo is not None and await s.mongo.ping()
    endpoints = {ep.name: (ep.unavailable_reason() or "ready") for ep in s.gateway.config.ordered()}
    llm_ok = any(v == "ready" for v in endpoints.values())
    body = {
        "status": "ready" if mongo_ok and llm_ok else "not_ready",
        "mongo": "ok" if mongo_ok else "unreachable",
        "llm_endpoints": endpoints,
        "skills": len(s.registry.list()),
        "skill_problems": len(s.registry.problems),
    }
    return JSONResponse(body, status_code=200 if mongo_ok and llm_ok else 503)
