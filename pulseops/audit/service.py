"""Append-only audit trail with a SHA-256 hash chain.

Each record's hash covers its content and the previous record's hash, so any
modification of history (e.g. by someone bypassing the database triggers) is
detectable with ``verify_chain``. Appends are serialised with a transaction-
scoped advisory lock; that limits audit write throughput, which is acceptable
for this workload and documented in docs/architecture.md.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from pulseops.audit.models import ActorType, AuditRecord
from pulseops.observability.logging import get_correlation_id, redact

GENESIS_HASH = "0" * 64
_AUDIT_LOCK_KEY = 0x50554C5345  # arbitrary constant: "PULSE"


def _canonical(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str, fields: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + _canonical(fields)).encode()).hexdigest()


def _hash_fields(rec: AuditRecord) -> dict[str, Any]:
    return {
        "occurred_at": rec.occurred_at.astimezone(UTC).isoformat(),
        "actor": rec.actor,
        "actor_type": rec.actor_type.value,
        "action": rec.action,
        "entity_type": rec.entity_type,
        "entity_id": rec.entity_id,
        "incident_id": str(rec.incident_id) if rec.incident_id else None,
        "correlation_id": rec.correlation_id,
        "details": rec.details,
    }


def record(
    session: Session,
    *,
    actor: str,
    actor_type: ActorType,
    action: str,
    entity_type: str,
    entity_id: str | uuid.UUID,
    incident_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditRecord:
    """Append an audit record inside the caller's transaction."""
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _AUDIT_LOCK_KEY})
    prev = session.scalar(select(AuditRecord.hash).order_by(AuditRecord.id.desc()).limit(1))
    # Round-trip through JSON so the hashed form equals what JSONB stores.
    safe_details = json.loads(_canonical(redact(details or {})))
    rec = AuditRecord(
        occurred_at=datetime.now(UTC),
        actor=actor,
        actor_type=actor_type,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        incident_id=incident_id,
        correlation_id=get_correlation_id(),
        details=safe_details,
        prev_hash=prev or GENESIS_HASH,
    )
    rec.hash = compute_hash(rec.prev_hash, _hash_fields(rec))
    session.add(rec)
    session.flush()
    return rec


@dataclass(frozen=True)
class ChainVerification:
    valid: bool
    records_checked: int
    first_invalid_id: int | None = None


def verify_chain(session: Session, batch_size: int = 1000) -> ChainVerification:
    prev = GENESIS_HASH
    checked = 0
    last_id = 0
    while True:
        rows = session.scalars(
            select(AuditRecord)
            .where(AuditRecord.id > last_id)
            .order_by(AuditRecord.id)
            .limit(batch_size)
        ).all()
        if not rows:
            return ChainVerification(True, checked)
        for rec in rows:
            if rec.prev_hash != prev or compute_hash(prev, _hash_fields(rec)) != rec.hash:
                return ChainVerification(False, checked, rec.id)
            prev = rec.hash
            checked += 1
            last_id = rec.id
