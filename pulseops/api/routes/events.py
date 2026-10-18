from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from pulseops.api.deps import DbSession, get_correlation_id, rate_limited, require
from pulseops.api.errors import ErrorResponse
from pulseops.api.pagination import Page, PageParams
from pulseops.auth.principal import Permission, Principal
from pulseops.ingestion.models import EventType, PipelineEvent, ProcessingStatus
from pulseops.ingestion.schemas import EventAccepted, EventIn, EventOut
from pulseops.ingestion.service import ingest_event

router = APIRouter(prefix="/api/v1/events", tags=["events"])


@router.post(
    "",
    status_code=202,
    response_model=EventAccepted,
    responses={
        200: {"model": EventAccepted, "description": "Duplicate delivery; original returned"},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "event_id reused with different payload"},
        413: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
    dependencies=[Depends(rate_limited("ingest"))],
    summary="Ingest a pipeline event (idempotent on source + event_id)",
)
def post_event(
    body: EventIn,
    session: DbSession,
    principal: Annotated[Principal, Depends(require(Permission.EVENTS_WRITE))],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> JSONResponse:
    result = ingest_event(session, body, correlation_id)
    session.commit()
    ev = result.event
    payload = EventAccepted(
        id=ev.id,
        event_id=ev.event_id,
        source=ev.source,
        status=ev.status,
        duplicate=not result.created,
        received_at=ev.received_at,
        correlation_id=ev.correlation_id,
        incident_id=ev.incident_id,
    )
    return JSONResponse(
        status_code=202 if result.created else 200, content=payload.model_dump(mode="json")
    )


@router.get("", response_model=Page[EventOut], summary="List ingested events")
def list_events(
    session: DbSession,
    _: Annotated[Principal, Depends(require(Permission.INCIDENTS_READ))],
    page: Annotated[PageParams, Depends()],
    status: Annotated[ProcessingStatus | None, Query()] = None,
    event_type: Annotated[EventType | None, Query()] = None,
) -> Page[EventOut]:
    stmt = select(PipelineEvent)
    if status:
        stmt = stmt.where(PipelineEvent.status == status)
    if event_type:
        stmt = stmt.where(PipelineEvent.event_type == event_type)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.order_by(PipelineEvent.received_at.desc(), PipelineEvent.id)
        .limit(page.limit)
        .offset(page.offset)
    ).all()
    return Page[EventOut](
        items=[EventOut.model_validate(r) for r in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )
