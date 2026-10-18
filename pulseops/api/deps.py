"""FastAPI dependencies: DB session, authentication, authorization, rate limits."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from pulseops.api.ratelimit import TokenBucketLimiter
from pulseops.auth.principal import Permission, Principal
from pulseops.auth.service import (
    authenticate_api_key,
    load_active_principal,
    verify_session_token,
)
from pulseops.errors import AuthenticationError, PermissionDeniedError
from pulseops.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


def get_session(runtime: Annotated[Runtime, Depends(get_runtime)]) -> Iterator[Session]:
    """Per-request session. Handlers commit explicitly; anything uncommitted is rolled back."""
    session = runtime.sessions()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


DbSession = Annotated[Session, Depends(get_session)]


def get_correlation_id(request: Request) -> str:
    return str(request.scope.get("state", {}).get("correlation_id", ""))


def get_principal(
    request: Request,
    session: DbSession,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> Principal:
    if x_api_key:
        principal = authenticate_api_key(session, x_api_key)
        session.commit()  # persists last_used_at
    elif authorization and authorization.lower().startswith("bearer "):
        subject = verify_session_token(
            authorization[7:].strip(), runtime.settings.session_secret_bytes()
        )
        # Re-check the principal on every request so deactivation takes effect immediately.
        principal = load_active_principal(session, subject)
    else:
        raise AuthenticationError("Missing credentials: use X-Api-Key or a Bearer session token")
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def require(permission: Permission) -> Callable[[Principal], Principal]:
    def _check(principal: CurrentPrincipal) -> Principal:
        if not principal.has(permission):
            raise PermissionDeniedError(
                f"Principal lacks permission {permission.value}",
                details={"required": permission.value},
            )
        return principal

    return _check


def rate_limited(kind: str) -> Callable[[Request], None]:
    def _limit(request: Request) -> None:
        limiters: dict[str, TokenBucketLimiter] = request.app.state.rate_limiters
        client = request.client.host if request.client else "unknown"
        if not limiters[kind].allow(f"{kind}:{client}"):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")

    return _limit
