"""FastAPI application. Run with ``uvicorn app.api.main:app``."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app import __version__
from app.api import health, org, skills, tools
from app.api.errors import install_error_handlers
from app.bootstrap import build_services
from app.core.config import Settings, get_settings
from app.core.logging import request_id_var, setup_logging

log = logging.getLogger("dayline.api")


def create_app(settings: Settings | None = None, *, services=None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if services is not None:  # tests inject a prepared service graph
            app.state.services = services
            yield
            return
        setup_logging(settings.log_level, settings.log_format)
        settings.check_required()
        app.state.services = await build_services(settings, db="api")
        log.info(
            "api started", extra={"env": settings.env, "skills": len(app.state.services.registry.list())}
        )
        try:
            yield
        finally:
            await app.state.services.aclose()

    app = FastAPI(title="Dayline", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
            response.headers["x-request-id"] = rid
            return response
        finally:
            request_id_var.reset(token)

    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(skills.router)
    app.include_router(org.router)
    app.include_router(tools.router)
    return app


app = create_app()
