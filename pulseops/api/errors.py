"""Consistent error schema and exception handlers."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from pulseops.errors import (
    AuthenticationError,
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from pulseops.observability.logging import get_logger

log = get_logger("pulseops.api.errors")


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None
    correlation_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


def _cid(request: Request) -> str | None:
    cid = request.scope.get("state", {}).get("correlation_id")
    return str(cid) if cid else None


def error_response(
    request: Request, status: int, code: str, message: str, details: Any = None
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, details=details, correlation_id=_cid(request))
    )
    headers = {"WWW-Authenticate": "ApiKey"} if status == 401 else None
    return JSONResponse(status_code=status, content=body.model_dump(), headers=headers)


_STATUS_BY_TYPE: list[tuple[type[DomainError], int]] = [
    (NotFoundError, 404),
    (ConflictError, 409),
    (ValidationFailedError, 422),
    (PermissionDeniedError, 403),
    (AuthenticationError, 401),
]


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError) -> JSONResponse:
        status = next((s for t, s in _STATUS_BY_TYPE if isinstance(exc, t)), 400)
        return error_response(request, status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        return error_response(
            request, 422, "validation_error", "Request validation failed", details
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed", 429: "rate_limited"}.get(
            exc.status_code, "http_error"
        )
        return error_response(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", error_type=type(exc).__name__)
        return error_response(request, 500, "internal_error", "Internal server error")
