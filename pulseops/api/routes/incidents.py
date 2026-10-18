from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload

from pulseops.api import presenters
from pulseops.api.deps import DbSession, get_runtime, require
from pulseops.api.errors import ErrorResponse
from pulseops.api.pagination import Page, PageParams
from pulseops.api.schemas import (
    AuditOut,
    DatasetOut,
    EvidenceOut,
    ImpactOut,
    ImpactTaskOut,
    IncidentDetail,
    IncidentSummary,
    InvestigateRequest,
    InvestigationOut,
    ProposalCreate,
    ProposalOut,
    TimelineEntry,
    TransitionRequest,
)
from pulseops.audit.models import ActorType, AuditRecord
from pulseops.auth.principal import Permission, Principal
from pulseops.evidence.service import list_evidence
from pulseops.incidents.models import (
    Incident,
    IncidentCategory,
    IncidentEvidence,
    IncidentStatus,
)
from pulseops.incidents.service import get_incident, transition
from pulseops.ingestion.models import PipelineEvent, Severity
from pulseops.investigator.models import InvestigationRun
from pulseops.investigator.requests import request_investigation
from pulseops.pipelines.graph import downstream_impact, upstream_lineage
from pulseops.pipelines.models import Dataset, Pipeline, TaskExecution
from pulseops.remediation.models import ProposerType, RecoveryProposal
from pulseops.remediation.service import create_proposal
from pulseops.runtime import Runtime

router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])

Reader = Annotated[Principal, Depends(require(Permission.INCIDENTS_READ))]
ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.get("", response_model=Page[IncidentSummary], summary="List incidents")
def list_incidents(
    session: DbSession,
    _: Reader,
    page: Annotated[PageParams, Depends()],
    status: Annotated[list[IncidentStatus] | None, Query()] = None,
    severity: Annotated[list[Severity] | None, Query()] = None,
    category: Annotated[IncidentCategory | None, Query()] = None,
    pipeline: Annotated[str | None, Query(max_length=100)] = None,
    q: Annotated[str | None, Query(max_length=100, description="Search title")] = None,
    opened_after: Annotated[datetime | None, Query()] = None,
    sort: Annotated[str, Query(pattern="^(-?opened_at|-?severity|-?updated_at)$")] = "-opened_at",
) -> Page[IncidentSummary]:
    stmt = select(Incident).join(Pipeline, Pipeline.id == Incident.pipeline_id)
    if status:
        stmt = stmt.where(Incident.status.in_(status))
    if severity:
        stmt = stmt.where(Incident.severity.in_(severity))
    if category:
        stmt = stmt.where(Incident.category == category)
    if pipeline:
        stmt = stmt.where(Pipeline.key == pipeline)
    if q:
        # Bound parameter; LIKE metacharacters in user input are escaped.
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(
            or_(
                Incident.title.ilike(f"%{escaped}%", escape="\\"),
                Incident.fingerprint.ilike(f"%{escaped}%", escape="\\"),
            )
        )
    if opened_after:
        stmt = stmt.where(Incident.opened_at >= opened_after)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    field = sort.lstrip("-")
    if field == "severity":
        column = func.array_position(
            func.string_to_array("LOW,MEDIUM,HIGH,CRITICAL", ","), Incident.severity
        )
    else:
        column = getattr(Incident, field)
    order = column.desc() if sort.startswith("-") else column.asc()
    rows = session.scalars(
        stmt.options(joinedload(Incident.pipeline), joinedload(Incident.task))
        .order_by(order, Incident.number.desc())
        .limit(page.limit)
        .offset(page.offset)
    ).all()
    return Page[IncidentSummary](
        items=presenters.summaries(session, rows),
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{incident_id}", response_model=IncidentDetail, responses=ERRORS)
def get_incident_detail(incident_id: uuid.UUID, session: DbSession, _: Reader) -> IncidentDetail:
    return presenters.detail(session, get_incident(session, incident_id))


@router.get("/{incident_id}/evidence", response_model=list[EvidenceOut], responses=ERRORS)
def get_evidence(
    incident_id: uuid.UUID,
    session: DbSession,
    _: Reader,
    q: Annotated[str | None, Query(max_length=200, description="Full-text search")] = None,
    kind: Annotated[str | None, Query(max_length=32)] = None,
) -> list[EvidenceOut]:
    get_incident(session, incident_id)
    if q:
        rows = session.scalars(
            select(IncidentEvidence).where(
                IncidentEvidence.incident_id == incident_id,
                IncidentEvidence.search.op("@@")(func.websearch_to_tsquery("english", q)),
            )
        ).all()
        rows = sorted(rows, key=lambda e: int(e.code[1:]))
    else:
        rows = list_evidence(session, incident_id)
    if kind:
        rows = [r for r in rows if r.kind.value == kind]
    return [EvidenceOut.model_validate(r) for r in rows]


@router.get("/{incident_id}/timeline", response_model=list[TimelineEntry], responses=ERRORS)
def get_timeline(incident_id: uuid.UUID, session: DbSession, _: Reader) -> list[TimelineEntry]:
    get_incident(session, incident_id)
    entries: list[TimelineEntry] = []
    for ev in session.scalars(
        select(PipelineEvent).where(PipelineEvent.incident_id == incident_id)
    ):
        entries.append(
            TimelineEntry(
                at=ev.occurred_at,
                kind="event",
                title=f"{ev.event_type.value} ({ev.severity.value}) from {ev.source}",
                actor=ev.source,
                details={"event_id": ev.event_id, "id": str(ev.id), "status": ev.status.value},
            )
        )
    for rec in session.scalars(
        select(AuditRecord).where(AuditRecord.incident_id == incident_id).order_by(AuditRecord.id)
    ):
        entries.append(
            TimelineEntry(
                at=rec.occurred_at,
                kind="audit",
                title=rec.action,
                actor=rec.actor,
                details=rec.details,
            )
        )
    return sorted(entries, key=lambda e: e.at)


def _latest_status(session: DbSession, task_id: uuid.UUID) -> str | None:
    status = session.scalar(
        select(TaskExecution.status)
        .where(TaskExecution.task_id == task_id)
        .order_by(TaskExecution.started_at.desc())
        .limit(1)
    )
    return status.value if status else None


@router.get("/{incident_id}/impact", response_model=ImpactOut, responses=ERRORS)
def get_impact(incident_id: uuid.UUID, session: DbSession, _: Reader) -> ImpactOut:
    incident = get_incident(session, incident_id)
    if incident.task_id is None:
        return ImpactOut(origin=None, downstream=[], upstream=[], datasets=[])
    down = downstream_impact(session, incident.task_id)
    up = upstream_lineage(session, incident.task_id)

    def tasks(items: list) -> list[ImpactTaskOut]:  # type: ignore[type-arg]
        return [
            ImpactTaskOut(
                task=t.qualified_key,
                depth=t.depth,
                path=t.path,
                datasets=t.datasets,
                latest_status=_latest_status(session, t.task_id),
            )
            for t in items
        ]

    datasets = session.scalars(select(Dataset).where(Dataset.key.in_(down.datasets))).all()
    return ImpactOut(
        origin=down.origin,
        downstream=tasks(down.tasks),
        upstream=tasks(up.tasks),
        datasets=[
            DatasetOut(key=d.key, is_stale=d.is_stale, stale_reason=d.stale_reason)
            for d in sorted(datasets, key=lambda d: d.key)
        ],
    )


@router.get(
    "/{incident_id}/investigations", response_model=list[InvestigationOut], responses=ERRORS
)
def list_investigations(
    incident_id: uuid.UUID, session: DbSession, _: Reader
) -> list[InvestigationOut]:
    get_incident(session, incident_id)
    runs = session.scalars(
        select(InvestigationRun)
        .where(InvestigationRun.incident_id == incident_id)
        .order_by(InvestigationRun.created_at.desc())
    ).all()
    return presenters.investigations(session, runs)


@router.post(
    "/{incident_id}/investigate",
    status_code=202,
    response_model=InvestigationOut,
    responses=ERRORS,
    summary="Request a new investigation (runs asynchronously)",
)
def investigate(
    incident_id: uuid.UUID,
    session: DbSession,
    principal: Annotated[Principal, Depends(require(Permission.INVESTIGATE))],
    runtime: Annotated[Runtime, Depends(get_runtime)],
    body: InvestigateRequest | None = None,
) -> InvestigationOut:
    incident = get_incident(session, incident_id, lock=True)
    run = request_investigation(
        session,
        incident,
        settings=runtime.settings,
        requested_by=principal.name,
        actor_type=ActorType.USER,
        trigger="manual",
    )
    session.commit()
    return presenters.investigations(session, [run])[0]


@router.get("/{incident_id}/proposals", response_model=list[ProposalOut], responses=ERRORS)
def list_proposals(incident_id: uuid.UUID, session: DbSession, _: Reader) -> list[ProposalOut]:
    get_incident(session, incident_id)
    rows = session.scalars(
        select(RecoveryProposal)
        .where(RecoveryProposal.incident_id == incident_id)
        .order_by(RecoveryProposal.created_at.desc())
    ).all()
    return presenters.proposals(session, rows)


@router.post(
    "/{incident_id}/proposals",
    status_code=201,
    response_model=ProposalOut,
    responses=ERRORS,
    summary="Create a human-authored recovery proposal (requires a second person to approve)",
)
def create_human_proposal(
    incident_id: uuid.UUID,
    body: ProposalCreate,
    session: DbSession,
    principal: Annotated[Principal, Depends(require(Permission.PROPOSALS_CREATE))],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> ProposalOut:
    incident = get_incident(session, incident_id, lock=True)
    proposal = create_proposal(
        session,
        incident,
        action_type=body.action_type,
        parameters=body.parameters,
        rationale=body.rationale,
        evidence_codes=body.evidence_codes,
        proposed_by=principal.name,
        proposer_type=ProposerType.HUMAN,
        actor_type=ActorType.USER,
        settings=runtime.settings,
    )
    session.commit()
    return presenters.proposals(session, [proposal])[0]


@router.post(
    "/{incident_id}/transitions",
    response_model=IncidentSummary,
    responses=ERRORS,
    summary="Manually resolve, close or reopen an incident",
)
def manual_transition(
    incident_id: uuid.UUID,
    body: TransitionRequest,
    session: DbSession,
    principal: Annotated[Principal, Depends(require(Permission.INCIDENTS_TRANSITION))],
) -> IncidentSummary:
    incident = get_incident(session, incident_id, lock=True)
    transition(
        session,
        incident,
        body.status,
        actor=principal.name,
        actor_type=ActorType.USER,
        reason=body.reason,
        manual=True,
    )
    session.commit()
    return presenters.summaries(session, [get_incident(session, incident_id)])[0]


@router.get("/{incident_id}/audit", response_model=list[AuditOut], responses=ERRORS)
def incident_audit(
    incident_id: uuid.UUID,
    session: DbSession,
    _: Annotated[Principal, Depends(require(Permission.AUDIT_READ))],
) -> list[AuditOut]:
    rows = session.scalars(
        select(AuditRecord).where(AuditRecord.incident_id == incident_id).order_by(AuditRecord.id)
    ).all()
    return [AuditOut.model_validate(r) for r in rows]
