"""Evidence collection and retrieval.

Every piece of information that can inform an investigation is stored as an
immutable ``IncidentEvidence`` row with a short per-incident code (E1, E2…).
Investigations may only cite these codes, which makes every conclusion
traceable to stored data. Content is redacted and size-capped on the way in.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pulseops.incidents.models import EvidenceKind, Incident, IncidentEvidence
from pulseops.ingestion.models import PipelineEvent
from pulseops.observability.logging import redact, redact_text
from pulseops.pipelines.graph import downstream_impact, upstream_lineage
from pulseops.pipelines.models import ExecutionStatus, LogRecord, TaskExecution

MAX_CONTENT_CHARS = 4000
LOG_WINDOW = timedelta(hours=6)
MAX_LOGS = 25
MAX_EXECUTIONS = 10
BASELINE_RUNS = 20


def _lock_incident(session: Session, incident_id: uuid.UUID) -> None:
    session.execute(select(Incident.id).where(Incident.id == incident_id).with_for_update())


def add_evidence(
    session: Session,
    incident: Incident,
    *,
    kind: EvidenceKind,
    source_ref: str,
    title: str,
    content: str,
    data: dict[str, Any] | None = None,
    observed_at: datetime | None = None,
    collected_by: str = "system",
) -> IncidentEvidence:
    """Idempotent on (incident, source_ref). Codes are allocated under the incident row lock."""
    _lock_incident(session, incident.id)
    existing = session.scalar(
        select(IncidentEvidence).where(
            IncidentEvidence.incident_id == incident.id,
            IncidentEvidence.source_ref == source_ref,
        )
    )
    if existing:
        return existing
    count = session.scalar(select(func.count()).where(IncidentEvidence.incident_id == incident.id))
    content = redact_text(content)
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + " …[truncated]"
    ev = IncidentEvidence(
        incident_id=incident.id,
        code=f"E{(count or 0) + 1}",
        kind=kind,
        source_ref=source_ref[:160],
        title=redact_text(title)[:300],
        content=content,
        data=redact(data or {}),
        observed_at=observed_at,
        collected_by=collected_by,
    )
    session.add(ev)
    session.flush()
    return ev


def list_evidence(session: Session, incident_id: uuid.UUID) -> list[IncidentEvidence]:
    rows = session.scalars(
        select(IncidentEvidence).where(IncidentEvidence.incident_id == incident_id)
    ).all()
    return sorted(rows, key=lambda e: int(e.code[1:]))


# --------------------------------------------------------------------------- collectors


def add_event_evidence(session: Session, incident: Incident, event: PipelineEvent) -> None:
    task_key = event.event_metadata.get("task_qualified") or ""
    lines = [
        f"event_type={event.event_type.value} severity={event.severity.value} "
        f"source={event.source} event_id={event.event_id}",
    ]
    if event.execution:
        lines.append(f"execution={event.execution}")
    if event.event_metadata:
        lines.append(f"metadata={event.event_metadata}")
    if event.log_ref:
        lines.append(f"log_ref={event.log_ref}")
    add_evidence(
        session,
        incident,
        kind=EvidenceKind.EVENT,
        source_ref=f"event:{event.id}",
        title=f"{event.event_type.value} event {task_key}".strip(),
        content="\n".join(lines),
        data={"event_pk": str(event.id), "event_type": event.event_type.value},
        observed_at=event.occurred_at,
    )
    if event.log_excerpt:
        add_evidence(
            session,
            incident,
            kind=EvidenceKind.LOG,
            source_ref=f"event-log:{event.id}",
            title=f"Log excerpt attached to {event.event_type.value} event",
            content=event.log_excerpt,
            data={"event_pk": str(event.id)},
            observed_at=event.occurred_at,
        )


def add_log_evidence(
    session: Session, incident: Incident, logs: list[LogRecord], collected_by: str = "system"
) -> list[IncidentEvidence]:
    out = []
    for log in logs:
        out.append(
            add_evidence(
                session,
                incident,
                kind=EvidenceKind.LOG,
                source_ref=f"log:{log.id}",
                title=f"{log.level} log from task",
                content=f"[{log.ts.isoformat()}] {log.level} {log.message}",
                data={"log_id": log.id, "level": log.level, "task_id": str(log.task_id)},
                observed_at=log.ts,
                collected_by=collected_by,
            )
        )
    return out


def recent_logs(
    session: Session,
    task_ids: list[uuid.UUID],
    since: datetime,
    query: str | None = None,
    limit: int = MAX_LOGS,
) -> list[LogRecord]:
    """Logs for the given tasks, optionally full-text filtered (parameterised tsquery)."""
    stmt = select(LogRecord).where(LogRecord.task_id.in_(task_ids), LogRecord.ts >= since)
    if query:
        stmt = stmt.where(LogRecord.search.op("@@")(func.websearch_to_tsquery("english", query)))
    # Prefer errors and warnings, then most recent.
    severity_rank = func.array_position(
        func.string_to_array("CRITICAL,ERROR,WARNING,WARN,INFO,DEBUG", ","), LogRecord.level
    )
    stmt = stmt.order_by(severity_rank.nulls_last(), LogRecord.ts.desc()).limit(limit)
    return list(session.scalars(stmt).all())


def add_execution_evidence(
    session: Session, incident: Incident, task_id: uuid.UUID, limit: int = MAX_EXECUTIONS
) -> list[IncidentEvidence]:
    execs = session.scalars(
        select(TaskExecution)
        .where(TaskExecution.task_id == task_id)
        .order_by(TaskExecution.started_at.desc())
        .limit(limit)
    ).all()
    out = []
    for ex in execs:
        duration = f"{ex.duration_seconds:.0f}s" if ex.duration_seconds is not None else "n/a"
        detail = f" error_type={ex.error_type} error={ex.error_message}" if ex.error_type else ""
        out.append(
            add_evidence(
                session,
                incident,
                kind=EvidenceKind.TASK_EXECUTION,
                source_ref=f"exec:{ex.id}:{ex.status.value}",
                title=f"Execution {ex.run_id} attempt {ex.attempt}: {ex.status.value}",
                content=(
                    f"run_id={ex.run_id} attempt={ex.attempt} status={ex.status.value} "
                    f"started_at={ex.started_at.isoformat()} duration={duration} "
                    f"triggered_by={ex.triggered_by}{detail}"
                ),
                data={
                    "execution_id": str(ex.id),
                    "status": ex.status.value,
                    "duration_seconds": ex.duration_seconds,
                    "triggered_by": ex.triggered_by,
                },
                observed_at=ex.started_at,
            )
        )
    return out


@dataclass(frozen=True)
class Baseline:
    samples: int
    mean: float
    stdev: float
    p95: float
    current: float | None
    z_score: float | None


def compute_baseline(session: Session, task_id: uuid.UUID) -> Baseline | None:
    execs = session.scalars(
        select(TaskExecution)
        .where(TaskExecution.task_id == task_id, TaskExecution.duration_seconds.is_not(None))
        .order_by(TaskExecution.started_at.desc())
        .limit(BASELINE_RUNS + 1)
    ).all()
    if len(execs) < 3:
        return None
    current, history = execs[0], [e for e in execs[1:] if e.status == ExecutionStatus.SUCCEEDED]
    durations = [e.duration_seconds for e in history if e.duration_seconds is not None]
    if len(durations) < 3:
        return None
    mean = statistics.fmean(durations)
    stdev = statistics.pstdev(durations)
    p95 = sorted(durations)[max(0, round(0.95 * len(durations)) - 1)]
    cur = current.duration_seconds
    z = (cur - mean) / stdev if cur is not None and stdev > 0 else None
    return Baseline(len(durations), mean, stdev, p95, cur, z)


def add_baseline_evidence(
    session: Session, incident: Incident, task_id: uuid.UUID, task_key: str
) -> IncidentEvidence | None:
    b = compute_baseline(session, task_id)
    if b is None:
        return None
    z = f"{b.z_score:.1f}" if b.z_score is not None else "n/a"
    cur = f"{b.current:.0f}s" if b.current is not None else "n/a"
    return add_evidence(
        session,
        incident,
        kind=EvidenceKind.BASELINE,
        source_ref=f"baseline:{task_id}:{b.samples}:{b.current}",
        title=f"Runtime baseline for {task_key}",
        content=(
            f"Last {b.samples} successful runs: mean={b.mean:.0f}s stdev={b.stdev:.0f}s "
            f"p95={b.p95:.0f}s. Most recent run duration={cur} (z-score={z})."
        ),
        data={
            "samples": b.samples,
            "mean_seconds": round(b.mean, 1),
            "stdev_seconds": round(b.stdev, 1),
            "p95_seconds": round(b.p95, 1),
            "current_seconds": b.current,
            "z_score": round(b.z_score, 2) if b.z_score is not None else None,
        },
    )


def add_dependency_evidence(
    session: Session, incident: Incident, task_id: uuid.UUID
) -> list[IncidentEvidence]:
    out: list[IncidentEvidence] = []
    lineage = upstream_lineage(session, task_id)
    for up in lineage.tasks:
        latest = session.scalar(
            select(TaskExecution)
            .where(TaskExecution.task_id == up.task_id)
            .order_by(TaskExecution.started_at.desc())
            .limit(1)
        )
        status = latest.status.value if latest else "NO_EXECUTIONS"
        out.append(
            add_evidence(
                session,
                incident,
                kind=EvidenceKind.DEPENDENCY,
                source_ref=f"upstream:{up.task_id}:{latest.id if latest else 'none'}",
                title=f"Upstream {up.qualified_key}: latest run {status}",
                content=(
                    f"Upstream task {up.qualified_key} (depth {up.depth}, path "
                    f"{' -> '.join(up.path)}) latest execution status={status}"
                    + (f" at {latest.started_at.isoformat()}" if latest else "")
                ),
                data={"task": up.qualified_key, "status": status, "depth": up.depth},
                observed_at=latest.started_at if latest else None,
            )
        )
    impact = downstream_impact(session, task_id)
    if impact.tasks:
        lines = [
            f"{t.qualified_key} (depth {t.depth}): {' -> '.join(t.path)}" for t in impact.tasks
        ]
        out.append(
            add_evidence(
                session,
                incident,
                kind=EvidenceKind.DEPENDENCY,
                source_ref=f"downstream:{task_id}:{len(impact.tasks)}:{','.join(impact.datasets)}"[
                    :160
                ],
                title=f"Downstream dependents of {impact.origin} ({len(impact.tasks)} tasks)",
                content="Downstream tasks:\n"
                + "\n".join(lines)
                + f"\nDatasets produced along these paths: {', '.join(impact.datasets) or 'none'}",
                data={"tasks": impact.task_keys, "datasets": impact.datasets},
            )
        )
    return out


def collect_for_incident(session: Session, incident: Incident) -> list[IncidentEvidence]:
    """Gather the standard evidence set for an incident (idempotent)."""
    events = session.scalars(
        select(PipelineEvent)
        .where(PipelineEvent.incident_id == incident.id)
        .order_by(PipelineEvent.occurred_at)
    ).all()
    for ev in events:
        add_event_evidence(session, incident, ev)
    task_ids = {e.task_id for e in events if e.task_id is not None}
    if incident.task_id is not None:
        task_ids.add(incident.task_id)
    if incident.task_id:
        add_execution_evidence(session, incident, incident.task_id)
        task_key = incident.task.qualified_key if incident.task else str(incident.task_id)
        add_baseline_evidence(session, incident, incident.task_id, task_key)
        add_dependency_evidence(session, incident, incident.task_id)
    since = min([incident.opened_at, *[e.occurred_at for e in events]]) - LOG_WINDOW
    if task_ids:
        add_log_evidence(session, incident, recent_logs(session, sorted(task_ids), since))
    return list_evidence(session, incident.id)
