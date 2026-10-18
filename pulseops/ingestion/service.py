"""Idempotent event ingestion.

Within one transaction the event row and its outbox message are written
together, so an accepted event is never lost between persistence and queue
publication. Duplicate deliveries (same ``source`` + ``event_id``) return the
original record; reusing an ``event_id`` with a different payload is a 409.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from pulseops.errors import ConflictError
from pulseops.ingestion.models import PipelineEvent, ProcessingStatus
from pulseops.ingestion.schemas import EventIn
from pulseops.messaging import outbox
from pulseops.observability.logging import get_logger
from pulseops.observability.metrics import EVENTS_DUPLICATE, EVENTS_INGESTED
from pulseops.pipelines.repository import resolve_pipeline_and_task

log = get_logger("pulseops.ingestion")


@dataclass(frozen=True)
class IngestResult:
    event: PipelineEvent
    created: bool


def payload_hash(event: EventIn) -> str:
    canonical = json.dumps(event.payload_for_hash(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def ingest_event(session: Session, event: EventIn, correlation_id: str) -> IngestResult:
    pipeline, task = resolve_pipeline_and_task(session, event.pipeline_id, event.task_id)
    digest = payload_hash(event)
    cid = event.correlation_id or correlation_id

    stmt = (
        insert(PipelineEvent)
        .values(
            source=event.source,
            event_id=event.event_id,
            pipeline_id=pipeline.id,
            task_id=task.id if task else None,
            event_type=event.event_type,
            severity=event.severity,
            occurred_at=event.occurred_at,
            correlation_id=cid,
            run_id=event.execution.run_id if event.execution else None,
            event_metadata=event.metadata,
            execution=event.execution.model_dump(mode="json") if event.execution else None,
            log_excerpt=event.log_excerpt,
            log_ref=event.log_ref,
            payload_hash=digest,
            status=ProcessingStatus.RECEIVED,
            attempts=0,
        )
        .on_conflict_do_nothing(constraint="uq_pipeline_event_source_event_id")
        .returning(PipelineEvent.id)
    )
    new_id = session.execute(stmt).scalar_one_or_none()

    if new_id is None:
        existing = session.scalar(
            select(PipelineEvent).where(
                PipelineEvent.source == event.source, PipelineEvent.event_id == event.event_id
            )
        )
        if existing is None:  # pragma: no cover - conflict implies the row exists
            raise ConflictError("Event conflict could not be resolved")
        if existing.payload_hash != digest:
            raise ConflictError(
                "event_id was already used with a different payload",
                code="event_id_reused",
                details={"source": event.source, "event_id": event.event_id},
            )
        EVENTS_DUPLICATE.inc()
        log.info("event_duplicate", source=event.source, event_id=event.event_id)
        return IngestResult(existing, created=False)

    outbox.enqueue(
        session,
        outbox.EVENT_INGESTED,
        aggregate_type="pipeline_event",
        aggregate_id=str(new_id),
        payload={"event_pk": str(new_id)},
        correlation_id=cid,
    )
    created = session.get_one(PipelineEvent, new_id)
    EVENTS_INGESTED.labels(event.event_type.value).inc()
    log.info(
        "event_ingested",
        source=event.source,
        event_id=event.event_id,
        event_type=event.event_type.value,
        pipeline=event.pipeline_id,
    )
    return IngestResult(created, created=True)
