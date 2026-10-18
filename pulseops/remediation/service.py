"""Proposal lifecycle: create -> human decision -> simulated execution.

Safety properties enforced here (and by database constraints):
- only allowlisted, in-scope, validated actions can be proposed;
- at most one in-flight proposal per incident (partial unique index);
- the proposer can never approve their own proposal; the AI identity cannot
  authenticate at all;
- a proposal is decided at most once (unique approval_decision.proposal_id);
- approval is rejected if the proposal expired, the incident changed since the
  proposal was made (version check), or preconditions no longer hold;
- a proposal executes at most once (unique recovery_execution.proposal_id),
  and preconditions are re-checked immediately before execution.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pulseops.audit import service as audit
from pulseops.audit.models import ActorType
from pulseops.auth.principal import Principal
from pulseops.config import Settings
from pulseops.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from pulseops.evidence.service import list_evidence
from pulseops.incidents.models import Incident, IncidentStatus
from pulseops.incidents.service import get_incident, transition
from pulseops.messaging import outbox
from pulseops.observability.metrics import RECOVERY_EXECUTIONS
from pulseops.remediation.actions import ACTIONS, build_scope, validate_action
from pulseops.remediation.models import (
    ActionType,
    ApprovalDecision,
    Decision,
    ExecutionOutcome,
    ProposalStatus,
    ProposerType,
    RecoveryExecution,
    RecoveryProposal,
)
from pulseops.remediation.port import ExecutionSandbox

EXECUTOR_ACTOR = "system:executor"


def evidence_hash(session: Session, incident_id: uuid.UUID, codes: list[str]) -> str:
    by_code = {e.code: e for e in list_evidence(session, incident_id)}
    material = "|".join(f"{c}:{by_code[c].id}" for c in sorted(set(codes)) if c in by_code)
    return hashlib.sha256(material.encode()).hexdigest()


def get_proposal(
    session: Session, proposal_id: uuid.UUID, *, lock: bool = False
) -> RecoveryProposal:
    stmt = select(RecoveryProposal).where(RecoveryProposal.id == proposal_id)
    if lock:
        stmt = stmt.with_for_update()
    proposal = session.scalar(stmt)
    if proposal is None:
        raise NotFoundError(f"Proposal {proposal_id} not found")
    return proposal


def create_proposal(
    session: Session,
    incident: Incident,
    *,
    action_type: ActionType | str,
    parameters: dict[str, Any],
    rationale: str,
    evidence_codes: list[str],
    proposed_by: str,
    proposer_type: ProposerType,
    actor_type: ActorType,
    settings: Settings,
    investigation_run_id: uuid.UUID | None = None,
) -> RecoveryProposal:
    """Caller must hold the incident row lock."""
    allowed_from = (
        (IncidentStatus.INVESTIGATING,)
        if proposer_type == ProposerType.AI
        else (IncidentStatus.OPEN,)
    )
    if incident.status not in allowed_from:
        raise ConflictError(
            f"Cannot create a proposal while incident is {incident.status}",
            code="invalid_state",
            details={"status": incident.status.value},
        )
    in_flight = session.scalar(
        select(RecoveryProposal.id).where(
            RecoveryProposal.incident_id == incident.id,
            RecoveryProposal.status.in_(
                [ProposalStatus.PENDING_APPROVAL, ProposalStatus.APPROVED, ProposalStatus.EXECUTING]
            ),
        )
    )
    if in_flight:
        raise ConflictError("Incident already has an in-flight proposal", code="proposal_exists")
    params = validate_action(action_type, parameters, build_scope(session, incident))
    known = {e.code for e in list_evidence(session, incident.id)}
    codes = sorted(set(evidence_codes))
    unknown = [c for c in codes if c not in known]
    if not codes or unknown:
        raise ValidationFailedError(
            "Proposals must cite existing evidence",
            code="invalid_evidence",
            details={"unknown": unknown},
        )

    transition(
        session,
        incident,
        IncidentStatus.ACTION_REQUIRED,
        actor=proposed_by,
        actor_type=actor_type,
        reason=f"{ActionType(action_type).value} proposed",
    )
    session.flush()
    now = datetime.now(UTC)
    proposal = RecoveryProposal(
        incident_id=incident.id,
        investigation_run_id=investigation_run_id,
        action_type=ActionType(action_type),
        parameters=params,
        rationale=rationale,
        evidence_codes=codes,
        proposed_by=proposed_by,
        proposer_type=proposer_type,
        status=ProposalStatus.PENDING_APPROVAL,
        incident_version=incident.version,
        evidence_hash=evidence_hash(session, incident.id, codes),
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(seconds=settings.proposal_ttl_seconds),
    )
    session.add(proposal)
    session.flush()
    audit.record(
        session,
        actor=proposed_by,
        actor_type=actor_type,
        action="proposal.created",
        entity_type="proposal",
        entity_id=proposal.id,
        incident_id=incident.id,
        details={
            "action_type": proposal.action_type.value,
            "parameters": params,
            "evidence_codes": codes,
            "incident_version": proposal.incident_version,
        },
    )
    return proposal


@dataclass(frozen=True)
class DecisionOutcome:
    proposal: RecoveryProposal
    accepted: bool
    error_code: str | None = None
    message: str | None = None
    details: Any = None


def _mark_stale(
    session: Session, proposal: RecoveryProposal, incident: Incident, principal: Principal, why: str
) -> None:
    proposal.status = ProposalStatus.STALE
    proposal.status_reason = why
    proposal.updated_at = datetime.now(UTC)
    if incident.status == IncidentStatus.ACTION_REQUIRED:
        transition(
            session,
            incident,
            IncidentStatus.OPEN,
            actor=principal.name,
            actor_type=ActorType.USER,
            reason=f"Proposal became stale: {why}",
        )
    audit.record(
        session,
        actor=principal.name,
        actor_type=ActorType.USER,
        action="proposal.stale",
        entity_type="proposal",
        entity_id=proposal.id,
        incident_id=incident.id,
        details={"reason": why},
    )


def decide(
    session: Session,
    proposal_id: uuid.UUID,
    *,
    principal: Principal,
    decision: Decision,
    reason: str | None,
    sandbox: ExecutionSandbox,
) -> DecisionOutcome:
    proposal = get_proposal(session, proposal_id, lock=True)
    incident = get_incident(session, proposal.incident_id, lock=True)

    if principal.name == proposal.proposed_by:
        raise PermissionDeniedError(
            "A proposal cannot be decided by the principal who proposed it",
            code="self_approval_forbidden",
        )
    if proposal.status != ProposalStatus.PENDING_APPROVAL:
        raise ConflictError(
            f"Proposal is {proposal.status}, not pending approval",
            code="proposal_not_pending",
            details={"status": proposal.status.value},
        )

    if decision == Decision.APPROVED:
        stale_reason = None
        if datetime.now(UTC) >= proposal.expires_at:
            stale_reason = "proposal expired"
        elif incident.version != proposal.incident_version:
            stale_reason = (
                f"incident changed since proposal (version {proposal.incident_version} -> "
                f"{incident.version})"
            )
        elif incident.status != IncidentStatus.ACTION_REQUIRED:
            stale_reason = f"incident is {incident.status}"
        if stale_reason:
            _mark_stale(session, proposal, incident, principal, stale_reason)
            return DecisionOutcome(proposal, False, "stale_proposal", stale_reason)
        checks = sandbox.check_preconditions(
            session, incident, proposal.action_type, proposal.parameters
        )
        failed = [c for c in checks if not c.ok]
        if failed:
            why = "preconditions failed: " + "; ".join(f"{c.name}: {c.detail}" for c in failed)
            _mark_stale(session, proposal, incident, principal, why)
            return DecisionOutcome(
                proposal, False, "precondition_failed", why, [c.as_dict() for c in checks]
            )

    try:
        with session.begin_nested():
            session.add(
                ApprovalDecision(
                    proposal_id=proposal.id,
                    decision=decision,
                    decided_by=principal.name,
                    reason=reason,
                )
            )
    except IntegrityError as exc:
        raise ConflictError("Proposal was already decided", code="proposal_not_pending") from exc

    proposal.updated_at = datetime.now(UTC)
    if decision == Decision.APPROVED:
        proposal.status = ProposalStatus.APPROVED
        transition(
            session,
            incident,
            IncidentStatus.RECOVERING,
            actor=principal.name,
            actor_type=ActorType.USER,
            reason=f"{proposal.action_type.value} approved",
        )
        outbox.enqueue(
            session,
            outbox.PROPOSAL_APPROVED,
            aggregate_type="proposal",
            aggregate_id=str(proposal.id),
            payload={"proposal_id": str(proposal.id)},
        )
    else:
        proposal.status = ProposalStatus.REJECTED
        proposal.status_reason = reason
        transition(
            session,
            incident,
            IncidentStatus.OPEN,
            actor=principal.name,
            actor_type=ActorType.USER,
            reason=f"{proposal.action_type.value} rejected",
        )
    audit.record(
        session,
        actor=principal.name,
        actor_type=ActorType.USER,
        action=f"proposal.{decision.value.lower()}",
        entity_type="proposal",
        entity_id=proposal.id,
        incident_id=incident.id,
        details={"reason": reason, "action_type": proposal.action_type.value},
    )
    return DecisionOutcome(proposal, True)


def execute_proposal(
    session: Session, proposal_id: uuid.UUID, sandbox: ExecutionSandbox
) -> RecoveryExecution | None:
    """Run an approved proposal exactly once. Returns None when there is nothing to do."""
    proposal = get_proposal(session, proposal_id, lock=True)
    if proposal.status != ProposalStatus.APPROVED:
        return None  # already executed / executing / not approved: idempotent no-op
    incident = get_incident(session, proposal.incident_id, lock=True)
    exec_id = session.execute(
        insert(RecoveryExecution)
        .values(
            id=uuid.uuid4(),
            proposal_id=proposal.id,
            status=ExecutionOutcome.RUNNING,
            started_at=datetime.now(UTC),
            preconditions=[],
            output={},
        )
        .on_conflict_do_nothing(index_elements=["proposal_id"])
        .returning(RecoveryExecution.id)
    ).scalar_one_or_none()
    if exec_id is None:
        return None
    execution = session.get_one(RecoveryExecution, exec_id)
    proposal.status = ProposalStatus.EXECUTING
    action = proposal.action_type
    spec = ACTIONS[action]
    audit.record(
        session,
        actor=EXECUTOR_ACTOR,
        actor_type=ActorType.SYSTEM,
        action="execution.started",
        entity_type="execution",
        entity_id=execution.id,
        incident_id=incident.id,
        details={"proposal_id": str(proposal.id), "action_type": action.value},
    )

    checks = sandbox.check_preconditions(session, incident, action, proposal.parameters)
    execution.preconditions = [c.as_dict() for c in checks]
    if not all(c.ok for c in checks):
        execution.status = ExecutionOutcome.PRECONDITION_FAILED
        execution.error = "; ".join(f"{c.name}: {c.detail}" for c in checks if not c.ok)
        proposal.status = ProposalStatus.FAILED
        outcome_status = IncidentStatus.OPEN
        result_action = "execution.precondition_failed"
    else:
        result = sandbox.execute(session, incident, action, proposal.parameters, str(proposal.id))
        execution.output = result.output
        execution.postcondition_ok = result.postcondition_ok
        execution.error = result.error
        ok = result.success and result.postcondition_ok
        execution.status = ExecutionOutcome.SUCCEEDED if ok else ExecutionOutcome.FAILED
        proposal.status = ProposalStatus.SUCCEEDED if ok else ProposalStatus.FAILED
        outcome_status = (
            IncidentStatus.RESOLVED if ok and spec.resolves_incident else IncidentStatus.OPEN
        )
        result_action = "execution.succeeded" if ok else "execution.failed"

    execution.finished_at = datetime.now(UTC)
    proposal.updated_at = execution.finished_at
    RECOVERY_EXECUTIONS.labels(action.value, execution.status.value).inc()
    audit.record(
        session,
        actor=EXECUTOR_ACTOR,
        actor_type=ActorType.SYSTEM,
        action=result_action,
        entity_type="execution",
        entity_id=execution.id,
        incident_id=incident.id,
        details={
            "action_type": action.value,
            "status": execution.status.value,
            "postcondition_ok": execution.postcondition_ok,
            "error": execution.error,
        },
    )
    transition(
        session,
        incident,
        outcome_status,
        actor=EXECUTOR_ACTOR,
        actor_type=ActorType.SYSTEM,
        reason=f"{action.value} {execution.status.value.lower()}",
    )
    session.flush()
    return execution


def mark_execution_failed(session: Session, proposal_id: uuid.UUID, error: str) -> None:
    """Failure hook after retries are exhausted (the execution transaction rolled back)."""
    proposal = get_proposal(session, proposal_id, lock=True)
    if proposal.status not in (ProposalStatus.APPROVED, ProposalStatus.EXECUTING):
        return
    incident = get_incident(session, proposal.incident_id, lock=True)
    proposal.status = ProposalStatus.FAILED
    proposal.status_reason = error[:1000]
    if incident.status == IncidentStatus.RECOVERING:
        transition(
            session,
            incident,
            IncidentStatus.OPEN,
            actor=EXECUTOR_ACTOR,
            actor_type=ActorType.SYSTEM,
            reason="Execution failed after retries",
        )
    audit.record(
        session,
        actor=EXECUTOR_ACTOR,
        actor_type=ActorType.SYSTEM,
        action="execution.dead_lettered",
        entity_type="proposal",
        entity_id=proposal.id,
        incident_id=incident.id,
        details={"error": error[:500]},
    )
