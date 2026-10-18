"""Append-only, hash-chained audit records.

The application role has no UPDATE/DELETE/TRUNCATE privilege on this table and
database triggers reject those statements for every role (see migration 0001).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pulseops.db.base import Base, created_at_col, str_enum


class ActorType(enum.StrEnum):
    USER = "USER"
    SERVICE = "SERVICE"
    AI = "AI"
    SYSTEM = "SYSTEM"


class AuditRecord(Base):
    __tablename__ = "audit_record"
    __table_args__ = (
        Index("ix_audit_record_incident", "incident_id", "id"),
        Index("ix_audit_record_entity", "entity_type", "entity_id"),
        Index("ix_audit_record_action", "action"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    occurred_at: Mapped[datetime] = created_at_col()
    actor: Mapped[str] = mapped_column(String(100))
    actor_type: Mapped[ActorType] = mapped_column(str_enum(ActorType, "actor_type"))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64))
    incident_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incident.id"))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)
