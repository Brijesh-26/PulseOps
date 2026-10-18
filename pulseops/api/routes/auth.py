from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from pulseops.api.deps import CurrentPrincipal, DbSession, get_runtime, rate_limited
from pulseops.audit import service as audit
from pulseops.audit.models import ActorType
from pulseops.auth.service import authenticate_api_key, issue_session_token
from pulseops.errors import AuthenticationError
from pulseops.runtime import Runtime

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class SessionRequest(BaseModel):
    api_key: str = Field(min_length=24, max_length=200)


class SessionResponse(BaseModel):
    token: str
    token_type: str = "Bearer"  # noqa: S105 - token scheme name, not a secret
    expires_at: int
    principal: str
    roles: list[str]
    permissions: list[str]


class MeResponse(BaseModel):
    principal: str
    roles: list[str]
    permissions: list[str]


@router.post(
    "/session",
    response_model=SessionResponse,
    dependencies=[Depends(rate_limited("auth"))],
    summary="Exchange an API key for a short-lived session token (demo auth)",
)
def create_session(
    body: SessionRequest,
    session: DbSession,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> SessionResponse:
    try:
        principal = authenticate_api_key(session, body.api_key)
    except AuthenticationError:
        audit.record(
            session,
            actor="anonymous",
            actor_type=ActorType.USER,
            action="auth.login_failed",
            entity_type="principal",
            entity_id="unknown",
            details={"key_prefix": body.api_key[:8]},
        )
        session.commit()
        raise
    token = issue_session_token(
        principal, runtime.settings.session_secret_bytes(), runtime.settings.session_ttl_seconds
    )
    audit.record(
        session,
        actor=principal.name,
        actor_type=ActorType.USER,
        action="auth.session_created",
        entity_type="principal",
        entity_id=principal.name,
    )
    session.commit()
    return SessionResponse(
        token=token.token,
        expires_at=token.expires_at,
        principal=principal.name,
        roles=sorted(r.value for r in principal.roles),
        permissions=sorted(p.value for p in principal.permissions),
    )


@router.get("/me", response_model=MeResponse)
def me(principal: CurrentPrincipal) -> MeResponse:
    return MeResponse(
        principal=principal.name,
        roles=sorted(r.value for r in principal.roles),
        permissions=sorted(p.value for p in principal.permissions),
    )
