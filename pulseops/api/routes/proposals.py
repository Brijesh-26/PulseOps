from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from pulseops.api import presenters
from pulseops.api.deps import DbSession, get_runtime, require
from pulseops.api.errors import ErrorResponse, error_response
from pulseops.api.schemas import DecisionRequest, ProposalOut, RejectRequest
from pulseops.auth.principal import Permission, Principal
from pulseops.remediation.models import Decision
from pulseops.remediation.service import decide, get_proposal
from pulseops.runtime import Runtime

router = APIRouter(prefix="/api/v1/proposals", tags=["proposals"])

ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse, "description": "Stale, already decided or precondition failed"},
}
Decider = Annotated[Principal, Depends(require(Permission.PROPOSALS_DECIDE))]


@router.get("/{proposal_id}", response_model=ProposalOut, responses=ERRORS)
def get_one(
    proposal_id: uuid.UUID,
    session: DbSession,
    _: Annotated[Principal, Depends(require(Permission.INCIDENTS_READ))],
) -> ProposalOut:
    return presenters.proposals(session, [get_proposal(session, proposal_id)])[0]


def _decide(
    request: Request,
    session: DbSession,
    runtime: Runtime,
    proposal_id: uuid.UUID,
    principal: Principal,
    decision: Decision,
    reason: str | None,
) -> JSONResponse:
    outcome = decide(
        session,
        proposal_id,
        principal=principal,
        decision=decision,
        reason=reason,
        sandbox=runtime.sandbox,
    )
    # Commit even when the decision was refused: the STALE marking and its
    # audit record must persist.
    session.commit()
    if not outcome.accepted:
        return error_response(
            request,
            409,
            outcome.error_code or "conflict",
            outcome.message or "Proposal cannot be approved",
            outcome.details,
        )
    body = presenters.proposals(session, [outcome.proposal])[0]
    return JSONResponse(status_code=200, content=body.model_dump(mode="json"))


@router.post(
    "/{proposal_id}/approve",
    response_model=ProposalOut,
    responses=ERRORS,
    summary="Approve a proposal; execution then runs asynchronously in the simulator",
)
def approve(
    proposal_id: uuid.UUID,
    request: Request,
    session: DbSession,
    principal: Decider,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    body: DecisionRequest | None = None,
) -> JSONResponse:
    return _decide(
        request,
        session,
        runtime,
        proposal_id,
        principal,
        Decision.APPROVED,
        body.reason if body else None,
    )


@router.post("/{proposal_id}/reject", response_model=ProposalOut, responses=ERRORS)
def reject(
    proposal_id: uuid.UUID,
    body: RejectRequest,
    request: Request,
    session: DbSession,
    principal: Decider,
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> JSONResponse:
    return _decide(
        request, session, runtime, proposal_id, principal, Decision.REJECTED, body.reason
    )
