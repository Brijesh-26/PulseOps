"""Requesting an investigation (creates a PENDING run and an outbox message)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from pulseops.audit import service as audit
from pulseops.audit.models import ActorType
from pulseops.config import Settings
from pulseops.errors import ConflictError
from pulseops.incidents.models import Incident, IncidentStatus
from pulseops.incidents.service import transition
from pulseops.investigator.models import InvestigationRun, RunStatus
from pulseops.investigator.schema import SCHEMA_VERSION
from pulseops.messaging import outbox
from pulseops.remediation.models import ProposalStatus, RecoveryProposal

INVESTIGABLE = (IncidentStatus.OPEN, IncidentStatus.ACTION_REQUIRED)


def request_investigation(
    session: Session,
    incident: Incident,
    *,
    settings: Settings,
    requested_by: str,
    actor_type: ActorType,
    trigger: str,
) -> InvestigationRun:
    """Must be called with the incident row locked by the caller."""
    if incident.status not in INVESTIGABLE:
        raise ConflictError(
            f"Incident in status {incident.status} cannot be investigated",
            code="invalid_state",
            details={"status": incident.status.value},
        )
    active = session.scalar(
        select(InvestigationRun).where(
            InvestigationRun.incident_id == incident.id,
            InvestigationRun.status.in_([RunStatus.PENDING, RunStatus.RUNNING]),
        )
    )
    if active:
        raise ConflictError(
            "An investigation is already in progress",
            code="investigation_in_progress",
            details={"run_id": str(active.id)},
        )
    # A re-investigation supersedes any proposal that is still awaiting a decision.
    for proposal in session.scalars(
        select(RecoveryProposal).where(
            RecoveryProposal.incident_id == incident.id,
            RecoveryProposal.status == ProposalStatus.PENDING_APPROVAL,
        )
    ):
        proposal.status = ProposalStatus.SUPERSEDED
        proposal.status_reason = "Superseded by a new investigation"
        audit.record(
            session,
            actor=requested_by,
            actor_type=actor_type,
            action="proposal.superseded",
            entity_type="proposal",
            entity_id=proposal.id,
            incident_id=incident.id,
        )
    run = InvestigationRun(
        incident_id=incident.id,
        status=RunStatus.PENDING,
        trigger=trigger,
        requested_by=requested_by,
        provider=settings.investigator_provider,
        model=settings.anthropic_model if settings.investigator_provider == "anthropic" else None,
        schema_version=SCHEMA_VERSION,
        validation_issues=[],
        tool_calls=[],
    )
    session.add(run)
    session.flush()
    transition(
        session,
        incident,
        IncidentStatus.INVESTIGATING,
        actor=requested_by,
        actor_type=actor_type,
        reason=f"{trigger} investigation requested",
    )
    audit.record(
        session,
        actor=requested_by,
        actor_type=actor_type,
        action="investigation.requested",
        entity_type="investigation_run",
        entity_id=run.id,
        incident_id=incident.id,
        details={"trigger": trigger, "provider": run.provider},
    )
    outbox.enqueue(
        session,
        outbox.INVESTIGATION_REQUESTED,
        aggregate_type="investigation_run",
        aggregate_id=str(run.id),
        payload={"run_id": str(run.id)},
    )
    return run
