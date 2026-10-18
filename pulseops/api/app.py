"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from pulseops.api.errors import install_error_handlers
from pulseops.api.middleware import BodySizeLimitMiddleware, CorrelationIdMiddleware
from pulseops.config import Settings, get_settings
from pulseops.db.session import check_database
from pulseops.observability.logging import configure_logging, get_logger
from pulseops.runtime import Runtime

log = get_logger("pulseops.api")


def create_app(runtime: Runtime | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or (runtime.settings if runtime else get_settings())
    configure_logging(settings.log_level, settings.log_json)
    runtime = runtime or Runtime.from_settings(settings)

    app = FastAPI(
        title="PulseOps API",
        version="0.1.0",
        description=(
            "Ingests data pipeline events, manages incidents, runs evidence-linked "
            "investigations and gates simulated recovery behind human approval."
        ),
    )
    app.state.runtime = runtime

    install_error_handlers(app)

    # Middleware order: the last added runs first (outermost).
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-Id", "X-Api-Key"],
        expose_headers=["X-Correlation-Id"],
    )
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        """Liveness: the process is up. Does not touch dependencies."""
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"])
    def ready(request: Request) -> JSONResponse:
        """Readiness: database and queue are reachable."""
        rt: Runtime = request.app.state.runtime
        checks: dict[str, Any] = {}
        ok = True

        def _db() -> None:
            check_database(rt.engine)

        probes: tuple[tuple[str, Callable[[], None]], ...] = (
            ("database", _db),
            ("queue", rt.queue.check),
        )
        for name, probe in probes:
            try:
                probe()
                checks[name] = "ok"
            except Exception as exc:  # readiness must report, not raise
                ok = False
                checks[name] = f"error: {type(exc).__name__}"
                log.warning("readiness_check_failed", check=name, error=type(exc).__name__)
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ready" if ok else "not_ready", "checks": checks},
        )

    @app.get("/metrics", tags=["ops"], include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    from pulseops.api.routes import register_routes  # noqa: PLC0415

    register_routes(app)
    return app
