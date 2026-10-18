"""Writing to the transactional outbox (always inside the caller's transaction)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from pulseops.messaging.models import OutboxMessage
from pulseops.observability.logging import get_correlation_id

# Message types
EVENT_INGESTED = "event.ingested"
INVESTIGATION_REQUESTED = "investigation.requested"
PROPOSAL_APPROVED = "proposal.approved"


def enqueue(
    session: Session,
    message_type: str,
    *,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, Any],
    correlation_id: str | None = None,
) -> OutboxMessage:
    msg = OutboxMessage(
        message_type=message_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
        correlation_id=correlation_id or get_correlation_id(),
    )
    session.add(msg)
    session.flush()
    return msg
