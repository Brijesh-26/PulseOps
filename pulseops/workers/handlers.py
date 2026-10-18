"""Message handlers: one transaction per message, failure hooks per aggregate."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import update

from pulseops.errors import PermanentError
from pulseops.incidents.engine import process_event
from pulseops.ingestion.models import PipelineEvent, ProcessingStatus
from pulseops.messaging import outbox
from pulseops.messaging.queue import Envelope
from pulseops.runtime import Runtime
from pulseops.workers.worker import Handler


def payload_uuid(env: Envelope, key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(env.payload[key]))
    except (KeyError, ValueError) as exc:
        raise PermanentError(f"message payload missing valid {key!r}") from exc


def build_event_handler(runtime: Runtime) -> Handler:
    def handle(env: Envelope) -> None:
        with runtime.sessions() as session, session.begin():
            process_event(session, payload_uuid(env, "event_pk"), runtime.settings)

    def on_failure(env: Envelope, error: str, final: bool) -> None:
        try:
            event_pk = uuid.UUID(str(env.payload.get("event_pk")))
        except ValueError:
            return
        with runtime.sessions() as session, session.begin():
            session.execute(
                update(PipelineEvent)
                .where(
                    PipelineEvent.id == event_pk,
                    PipelineEvent.status != ProcessingStatus.PROCESSED,
                )
                .values(
                    status=ProcessingStatus.DEAD_LETTERED if final else ProcessingStatus.FAILED,
                    attempts=PipelineEvent.attempts + 1,
                    last_error=error,
                    processed_at=datetime.now(UTC) if final else None,
                )
            )

    return Handler(handle, on_failure)


def build_handlers(runtime: Runtime) -> dict[str, Handler]:
    from pulseops.investigator.handler import build_investigation_handler  # noqa: PLC0415
    from pulseops.remediation.handler import build_execution_handler  # noqa: PLC0415

    return {
        outbox.EVENT_INGESTED: build_event_handler(runtime),
        outbox.INVESTIGATION_REQUESTED: build_investigation_handler(runtime),
        outbox.PROPOSAL_APPROVED: build_execution_handler(runtime),
    }
