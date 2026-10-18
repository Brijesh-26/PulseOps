"""Incident lifecycle operations (always called inside a caller-owned transaction)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from pulseops.audit import service as audit
from pulseops.audit.models import ActorType
from pulseops.errors import NotFoundError
from pulseops.incidents.models import Incident, IncidentStatus
from pulseops.incidents.state import assert_transition


def get_incident(session: Session, incident_id: uuid.UUID, *, lock: bool = False) -> Incident:
    stmt = (
        select(Incident)
        .options(joinedload(Incident.pipeline), joinedload(Incident.task))
        .where(Incident.id == incident_id)
    )
    if lock:
        stmt = stmt.with_for_update(of=Incident)
    incident = session.scalar(stmt)
    if incident is None:
        raise NotFoundError(f"Incident {incident_id} not found")
    return incident


def transition(
    session: Session,
    incident: Incident,
    target: IncidentStatus,
    *,
    actor: str,
    actor_type: ActorType,
    reason: str,
    manual: bool = False,
) -> None:
    current = incident.status
    assert_transition(current, target, manual=manual)
    now = datetime.now(UTC)
    incident.status = target
    incident.updated_at = now
    if target == IncidentStatus.RESOLVED:
        incident.resolved_at = now
    elif target == IncidentStatus.CLOSED:
        incident.closed_at = now
    elif target == IncidentStatus.OPEN and current == IncidentStatus.RESOLVED:
        incident.resolved_at = None  # reopened
    session.flush()
    audit.record(
        session,
        actor=actor,
        actor_type=actor_type,
        action="incident.status_changed",
        entity_type="incident",
        entity_id=incident.id,
        incident_id=incident.id,
        details={"from": current.value, "to": target.value, "reason": reason},
    )
