from fastapi import FastAPI

from pulseops.api.ratelimit import TokenBucketLimiter


def register_routes(app: FastAPI) -> None:
    """Mount versioned API routers."""
    from pulseops.api.routes import (  # noqa: PLC0415
        audit,
        auth,
        demo,
        events,
        incidents,
        proposals,
        stats,
    )

    settings = app.state.runtime.settings
    app.state.rate_limiters = {
        "ingest": TokenBucketLimiter(settings.rate_limit_per_minute),
        "auth": TokenBucketLimiter(settings.auth_rate_limit_per_minute),
    }
    for module in (auth, events, incidents, proposals, audit, stats, demo):
        app.include_router(module.router)
