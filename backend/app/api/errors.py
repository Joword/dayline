"""Domain exceptions -> HTTP responses, in one place."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.crypto import CryptoError
from app.db.repositories.reports import ReportConflict
from app.escalations.service import EscalationError, EscalationForbidden, EscalationNotFound
from app.integrations.models import IntegrationError
from app.llm.types import NoEndpointAvailable
from app.relay.service import RelayForbidden, RelayNotFound
from app.reports.service import ReportError, ReportForbidden, ReportNotFound
from app.skills.registry import SkillNotFound
from app.skills.runner import SkillForbidden, SkillInputError, SkillRunError
from app.tools.service import ToolError, ToolForbidden, ToolNotFound


def install_error_handlers(app: FastAPI) -> None:
    def simple(status: int):
        async def handler(request: Request, exc: Exception) -> JSONResponse:
            return JSONResponse(
                {"detail": str(exc) if not isinstance(exc, KeyError) else f"unknown: {exc.args[0]}"},
                status_code=status,
            )

        return handler

    for exc, status in (
        (RelayForbidden, 403),
        (EscalationForbidden, 403),
        (EscalationNotFound, 404),
        (SkillForbidden, 403),
        (ReportForbidden, 403),
        (RelayNotFound, 404),
        (ReportNotFound, 404),
        (SkillNotFound, 404),
        (ReportConflict, 409),
        (SkillInputError, 422),
        (ReportError, 422),
        (EscalationError, 422),
        (CryptoError, 503),
        (IntegrationError, 502),
    ):
        app.add_exception_handler(exc, simple(status))

    async def run_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, SkillRunError)
        cause = exc.__cause__
        if isinstance(cause, NoEndpointAvailable):
            body = {"message": str(cause), "run_id": exc.run_id, "retry_later": cause.queued}
            return JSONResponse({"detail": body}, status_code=503)
        code = 422 if exc.status == "refused" else 502
        return JSONResponse(
            {"detail": {"message": str(exc), "run_id": exc.run_id, "status": exc.status}}, status_code=code
        )

    app.add_exception_handler(SkillRunError, run_error)

    async def tool_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ToolError)
        status = 403 if isinstance(exc, ToolForbidden) else 404 if isinstance(exc, ToolNotFound) else 422
        detail: str | dict = str(exc)
        if exc.findings:
            detail = {"message": str(exc), "findings": [f.__dict__ for f in exc.findings]}
        return JSONResponse({"detail": detail}, status_code=status)

    app.add_exception_handler(ToolError, tool_error)
