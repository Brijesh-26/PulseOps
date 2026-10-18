"""Incident engine: turns an ingested event into incident state.

Processing one event is a single transaction: execution history, the incident
upsert, evidence and the follow-up outbox message either all commit or none
do. Re-processing an already PROCESSED event is a no-op, which makes the
handler safe under at-least-once delivery.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from pulseops.audit import service as audit
from pulseops.audit.models import ActorType
from pulseops.config import Settings
from pulseops.errors import PermanentError
from pulseops.evidence.service import add_event_evidence
from pulseops.incidents.models import (
    ACTIVE_STATUSES,
    Incident,
    IncidentCategory,
    IncidentStatus,
)
from pulseops.incidents.service import get_incident
from pulseops.ingestion.models import EventType, PipelineEvent, ProcessingStatus, Severity
from pulseops.investigator.requests import request_investigation
from pulseops.pipelines.models import ExecutionStatus, LogRecord, Task, TaskExecution
from pulseops.pipelines.repository import get_task_by_qualified_key

ENGINE_ACTOR = "system:incident-engine"

_EXECUTION_STATUS = {
    EventType.TASK_FAILED: ExecutionStatus.FAILED,
    EventType.TASK_SUCCEEDED: ExecutionStatus.SUCCEEDED,
    EventType.DEPENDENCY_FAILED: ExecutionStatus.UPSTREAM_FAILED,
}


@dataclass(frozen=True)
class ProcessOutcome:
    event_id: uuid.UUID
    incident_id: uuid.UUID | None
    created_incident: bool
    skipped: bool = False


def _record_execution(session: Session, event: PipelineEvent) -> uuid.UUID | None:
    if not event.execution or event.task_id is None:
        return None
    ex = event.execution
    status = _EXECUTION_STATUS.get(event.event_type)
    if status is None:  # latency anomaly: running or finished-but-slow
        status = ExecutionStatus.SUCCEEDED if ex.get("finished_at") else ExecutionStatus.RUNNING
    started = datetime.fromisoformat(ex["started_at"])
    finished = datetime.fromisoformat(ex["finished_at"]) if ex.get("finished_at") else None
    duration = ex.get("duration_seconds")
    if duration is None and finished is not None:
        duration = (finished - started).total_seconds()
    values = {
        "task_id": event.task_id,
        "run_id": ex["run_id"],
        "attempt": ex.get("attempt", 1),
        "status": status,
        "started_at": started,
        "finished_at": finished,
        "duration_seconds": duration,
        "error_type": ex.get("error_type"),
        "error_message": (event.log_excerpt or "")[:500] or None
        if status == ExecutionStatus.FAILED
        else None,
        "triggered_by": event.source,
    }
    base = insert(TaskExecution).values(id=uuid.uuid4(), **values)
    stmt = base.on_conflict_do_update(
        constraint="uq_execution_run_attempt",
        set_={
            k: base.excluded[k] for k in ("status", "finished_at", "duration_seconds", "error_type")
        },
    ).returning(TaskExecution.id)
    exec_id: uuid.UUID = session.execute(stmt).scalar_one()
    return exec_id


def _record_log(session: Session, event: PipelineEvent, exec_id: uuid.UUID | None) -> None:
    if not event.log_excerpt or event.task_id is None:
        return
    level = "ERROR" if event.event_type == EventType.TASK_FAILED else "WARNING"
    session.add(
        LogRecord(
            task_id=event.task_id,
            task_execution_id=exec_id,
            ts=event.occurred_at,
            level=level,
            message=event.log_excerpt[:8192],
            source=f"event:{event.source}",
        )
    )


def _classify(
    session: Session, event: PipelineEvent, task: Task
) -> tuple[str, IncidentCategory, Task, str]:
    """Return (fingerprint, category, incident task, title)."""
    if event.event_type == EventType.DEPENDENCY_FAILED:
        upstream_key = str(event.event_metadata.get("upstream_task", ""))
        upstream = get_task_by_qualified_key(session, upstream_key)
        if upstream is None:
            raise PermanentError(f"DEPENDENCY_FAILED references unknown task {upstream_key!r}")
        return (
            f"{upstream.qualified_key}:TASK_FAILURE",
            IncidentCategory.DEPENDENCY_FAILURE,
            upstream,
            f"Upstream failure in {upstream.qualified_key} affecting downstream tasks",
        )
    if event.event_type == EventType.LATENCY_ANOMALY:
        return (
            f"{task.qualified_key}:LATENCY_ANOMALY",
            IncidentCategory.LATENCY_ANOMALY,
            task,
            f"Latency anomaly in {task.qualified_key}",
        )
    return (
        f"{task.qualified_key}:TASK_FAILURE",
        IncidentCategory.TASK_FAILURE,
        task,
        f"{task.qualified_key} failed",
    )


def _upsert_incident(
    session: Session,
    *,
    fingerprint: str,
    category: IncidentCategory,
    task: Task,
    title: str,
    severity: Severity,
    occurred_at: datetime,
) -> tuple[Incident, bool]:
    # Fast path (also avoids consuming identity numbers on conflicting inserts).
    active_id = session.scalar(
        select(Incident.id).where(
            Incident.fingerprint == fingerprint, Incident.status.in_(ACTIVE_STATUSES)
        )
    )
    if active_id is not None:
        return get_incident(session, active_id, lock=True), False
    stmt = (
        insert(Incident)
        .values(
            id=uuid.uuid4(),
            title=title,
            status=IncidentStatus.OPEN,
            severity=severity,
            category=category,
            fingerprint=fingerprint,
            pipeline_id=task.pipeline_id,
            task_id=task.id,
            opened_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            last_event_at=occurred_at,
            event_count=0,
            version=1,
        )
        .on_conflict_do_nothing(
            index_elements=["fingerprint"],
            index_where=text("status IN ('OPEN','INVESTIGATING','ACTION_REQUIRED','RECOVERING')"),
        )
        .returning(Incident.id)
    )
    new_id = session.execute(stmt).scalar_one_or_none()
    if new_id is not None:
        return get_incident(session, new_id, lock=True), True
    existing_id = session.scalar(
        select(Incident.id).where(
            Incident.fingerprint == fingerprint, Incident.status.in_(ACTIVE_STATUSES)
        )
    )
    if existing_id is None:  # resolved concurrently between insert and select; retry later
        from pulseops.errors import RetryableError  # noqa: PLC0415

        raise RetryableError("Incident for fingerprint changed state concurrently")
    return get_incident(session, existing_id, lock=True), False


def process_event(session: Session, event_pk: uuid.UUID, settings: Settings) -> ProcessOutcome:
    event = session.scalar(
        select(PipelineEvent).where(PipelineEvent.id == event_pk).with_for_update()
    )
    if event is None:
        raise PermanentError(f"Event {event_pk} does not exist")
    if event.status == ProcessingStatus.PROCESSED:
        return ProcessOutcome(event.id, event.incident_id, False, skipped=True)

    event.status = ProcessingStatus.PROCESSING
    exec_id = _record_execution(session, event)
    _record_log(session, event, exec_id)

    incident: Incident | None = None
    created = False
    task = session.get(Task, event.task_id) if event.task_id else None
    if event.event_type == EventType.TASK_SUCCEEDED:
        # Success never opens an incident; attach it as evidence to an active one if present.
        if task is not None:
            existing_id = session.scalar(
                select(Incident.id).where(
                    Incident.task_id == task.id, Incident.status.in_(ACTIVE_STATUSES)
                )
            )
            if existing_id:
                incident = get_incident(session, existing_id, lock=True)
                event.incident_id = incident.id
                add_event_evidence(session, incident, event)
    elif task is not None:
        fingerprint, category, incident_task, title = _classify(session, event, task)
        incident, created = _upsert_incident(
            session,
            fingerprint=fingerprint,
            category=category,
            task=incident_task,
            title=title,
            severity=event.severity,
            occurred_at=event.occurred_at,
        )
        incident.event_count += 1
        incident.last_event_at = max(incident.last_event_at, event.occurred_at)
        if event.severity.rank > incident.severity.rank:
            incident.severity = event.severity
        if event.event_type == EventType.DEPENDENCY_FAILED:
            incident.category = IncidentCategory.DEPENDENCY_FAILURE
        incident.updated_at = datetime.now(UTC)
        event.incident_id = incident.id
        session.flush()
        add_event_evidence(session, incident, event)
        if created:
            audit.record(
                session,
                actor=ENGINE_ACTOR,
                actor_type=ActorType.SYSTEM,
                action="incident.opened",
                entity_type="incident",
                entity_id=incident.id,
                incident_id=incident.id,
                details={
                    "fingerprint": fingerprint,
                    "category": category.value,
                    "severity": event.severity.value,
                    "trigger_event": str(event.id),
                },
            )
            request_investigation(
                session,
                incident,
                settings=settings,
                requested_by=ENGINE_ACTOR,
                actor_type=ActorType.SYSTEM,
                trigger="auto",
            )

    event.status = ProcessingStatus.PROCESSED
    event.attempts += 1
    event.processed_at = datetime.now(UTC)
    event.last_error = None
    session.flush()
    return ProcessOutcome(event.id, incident.id if incident else None, created)
