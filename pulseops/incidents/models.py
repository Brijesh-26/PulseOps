"""Incidents and the evidence collected for them."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Computed,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pulseops.db.base import Base, created_at_col, str_enum, tz, utcnow, uuid_pk
from pulseops.ingestion.models import Severity
from pulseops.pipelines.models import Pipeline, Task


class IncidentStatus(enum.StrEnum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    RECOVERING = "RECOVERING"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


ACTIVE_STATUSES = (
    IncidentStatus.OPEN,
    IncidentStatus.INVESTIGATING,
    IncidentStatus.ACTION_REQUIRED,
    IncidentStatus.RECOVERING,
)


class IncidentCategory(enum.StrEnum):
    TASK_FAILURE = "TASK_FAILURE"
    LATENCY_ANOMALY = "LATENCY_ANOMALY"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"


class EvidenceKind(enum.StrEnum):
    EVENT = "EVENT"
    LOG = "LOG"
    TASK_EXECUTION = "TASK_EXECUTION"
    BASELINE = "BASELINE"
    DEPENDENCY = "DEPENDENCY"
    DIAGNOSTIC = "DIAGNOSTIC"


class Incident(Base):
    __tablename__ = "incident"
    __table_args__ = (
        # At most one active incident per fingerprint: the dedup key for correlated events.
        Index(
            "uq_incident_active_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text(
                "status IN ('OPEN','INVESTIGATING','ACTION_REQUIRED','RECOVERING')"
            ),
        ),
        Index("ix_incident_status_opened", "status", "opened_at"),
        Index("ix_incident_pipeline", "pipeline_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    number: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[IncidentStatus] = mapped_column(
        str_enum(IncidentStatus, "incident_status"), default=IncidentStatus.OPEN
    )
    severity: Mapped[Severity] = mapped_column(str_enum(Severity, "incident_severity"))
    category: Mapped[IncidentCategory] = mapped_column(
        str_enum(IncidentCategory, "incident_category")
    )
    fingerprint: Mapped[str] = mapped_column(String(255))
    pipeline_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task.id"))
    opened_at: Mapped[datetime] = mapped_column(tz(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(tz(), default=utcnow, onupdate=utcnow)
    last_event_at: Mapped[datetime] = mapped_column(tz(), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(tz())
    closed_at: Mapped[datetime | None] = mapped_column(tz())
    event_count: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    # Optimistic locking: concurrent writers get StaleDataError instead of lost updates.
    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012

    pipeline: Mapped[Pipeline] = relationship()
    task: Mapped[Task | None] = relationship()

    @property
    def display_id(self) -> str:
        return f"INC-{self.number}"


class IncidentEvidence(Base):
    """An immutable piece of evidence, referenced by a short per-incident code (E1, E2…)."""

    __tablename__ = "incident_evidence"
    __table_args__ = (
        UniqueConstraint("incident_id", "code", name="uq_evidence_incident_code"),
        UniqueConstraint("incident_id", "source_ref", name="uq_evidence_incident_source"),
        Index("ix_incident_evidence_search", "search", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incident.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(16))
    kind: Mapped[EvidenceKind] = mapped_column(str_enum(EvidenceKind, "evidence_kind"))
    source_ref: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    observed_at: Mapped[datetime | None] = mapped_column(tz())
    collected_by: Mapped[str] = mapped_column(String(64), default="system")
    collected_at: Mapped[datetime] = created_at_col()
    search: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', title || ' ' || content)", persisted=True),
    )
