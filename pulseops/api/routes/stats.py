"""Dashboard metrics, computed from stored data on every request (nothing hardcoded)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import case, func, select

from pulseops.api.deps import DbSession, require
from pulseops.auth.principal import Permission, Principal
from pulseops.incidents.models import ACTIVE_STATUSES, Incident, IncidentStatus
from pulseops.ingestion.models import EventType, PipelineEvent, Severity
from pulseops.pipelines.models import ExecutionStatus, Pipeline, Task, TaskExecution
from pulseops.remediation.models import ProposalStatus, RecoveryProposal

router = APIRouter(prefix="/api/v1", tags=["stats"])
Reader = Annotated[Principal, Depends(require(Permission.INCIDENTS_READ))]


class PipelineReliability(BaseModel):
    pipeline: str
    name: str
    runs_7d: int
    failures_7d: int
    success_rate_7d: float | None
    open_incidents: int


class Stats(BaseModel):
    generated_at: datetime
    open_incidents: int
    critical_open_incidents: int
    by_status: dict[str, int]
    by_severity_open: dict[str, int]
    failures_24h: int
    recent_failures: list[dict[str, Any]]
    resolved_7d: int
    mean_time_to_resolve_minutes_30d: float | None
    pending_approvals: int
    pipelines: list[PipelineReliability]


@router.get("/stats", response_model=Stats, summary="Overview metrics for the dashboard")
def stats(session: DbSession, _: Reader) -> Stats:
    now = datetime.now(UTC)
    by_status = {
        s.value: c
        for s, c in session.execute(select(Incident.status, func.count()).group_by(Incident.status))
    }
    by_sev = {
        s.value: c
        for s, c in session.execute(
            select(Incident.severity, func.count())
            .where(Incident.status.in_(ACTIVE_STATUSES))
            .group_by(Incident.severity)
        )
    }
    failure_types = [EventType.TASK_FAILED, EventType.DEPENDENCY_FAILED]
    failures_24h = session.scalar(
        select(func.count()).where(
            PipelineEvent.event_type.in_(failure_types),
            PipelineEvent.occurred_at >= now - timedelta(hours=24),
        )
    )
    recent = session.execute(
        select(PipelineEvent, Pipeline.key, Task.key)
        .join(Pipeline, Pipeline.id == PipelineEvent.pipeline_id)
        .outerjoin(Task, Task.id == PipelineEvent.task_id)
        .where(PipelineEvent.event_type.in_(failure_types))
        .order_by(PipelineEvent.occurred_at.desc())
        .limit(8)
    ).all()
    resolved: list[tuple[datetime, datetime]] = [
        (o, r)
        for o, r in session.execute(
            select(Incident.opened_at, Incident.resolved_at).where(
                Incident.resolved_at.is_not(None),
                Incident.resolved_at >= now - timedelta(days=30),
            )
        )
        if r is not None
    ]
    mttr = (
        sum((r - o).total_seconds() for o, r in resolved) / len(resolved) / 60 if resolved else None
    )
    pending = session.scalar(
        select(func.count()).where(RecoveryProposal.status == ProposalStatus.PENDING_APPROVAL)
    )

    since = now - timedelta(days=7)
    runs = {
        key: (total, failed)
        for key, total, failed in session.execute(
            select(
                Pipeline.key,
                func.count(TaskExecution.id),
                func.sum(
                    case(
                        (
                            TaskExecution.status.in_(
                                [ExecutionStatus.FAILED, ExecutionStatus.UPSTREAM_FAILED]
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
            )
            .join(Task, Task.pipeline_id == Pipeline.id)
            .join(TaskExecution, TaskExecution.task_id == Task.id)
            .where(TaskExecution.started_at >= since)
            .group_by(Pipeline.key)
        )
    }
    open_by_pipeline = {
        pid: c
        for pid, c in session.execute(
            select(Incident.pipeline_id, func.count())
            .where(Incident.status.in_(ACTIVE_STATUSES))
            .group_by(Incident.pipeline_id)
        )
    }
    pipelines = []
    for p in session.scalars(select(Pipeline).order_by(Pipeline.key)):
        total, failed = runs.get(p.key, (0, 0))
        failed = int(failed or 0)
        pipelines.append(
            PipelineReliability(
                pipeline=p.key,
                name=p.name,
                runs_7d=total,
                failures_7d=failed,
                success_rate_7d=round((total - failed) / total, 4) if total else None,
                open_incidents=open_by_pipeline.get(p.id, 0),
            )
        )

    return Stats(
        generated_at=now,
        open_incidents=sum(by_status.get(s.value, 0) for s in ACTIVE_STATUSES),
        critical_open_incidents=by_sev.get(Severity.CRITICAL.value, 0),
        by_status={s.value: by_status.get(s.value, 0) for s in IncidentStatus},
        by_severity_open={s.value: by_sev.get(s.value, 0) for s in Severity},
        failures_24h=failures_24h or 0,
        recent_failures=[
            {
                "event_id": ev.event_id,
                "event_type": ev.event_type.value,
                "severity": ev.severity.value,
                "task": f"{pk}.{tk}" if tk else pk,
                "occurred_at": ev.occurred_at.isoformat(),
                "incident_id": str(ev.incident_id) if ev.incident_id else None,
            }
            for ev, pk, tk in recent
        ],
        resolved_7d=sum(1 for _, r in resolved if r >= now - timedelta(days=7)),
        mean_time_to_resolve_minutes_30d=round(mttr, 1) if mttr is not None else None,
        pending_approvals=pending or 0,
        pipelines=pipelines,
    )
