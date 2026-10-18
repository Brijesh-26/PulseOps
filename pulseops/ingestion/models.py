"""Persisted pipeline events (the ingestion log)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pulseops.db.base import Base, str_enum, tz, utcnow, uuid_pk


class EventType(enum.StrEnum):
    TASK_FAILED = "TASK_FAILED"
    TASK_SUCCEEDED = "TASK_SUCCEEDED"
    LATENCY_ANOMALY = "LATENCY_ANOMALY"
    DEPENDENCY_FAILED = "DEPENDENCY_FAILED"


class Severity(enum.StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return ["LOW", "MEDIUM", "HIGH", "CRITICAL"].index(self.value)


class ProcessingStatus(enum.StrEnum):
    RECEIVED = "RECEIVED"  # persisted, outbox message pending
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"  # retryable failure recorded; may still succeed on redelivery
    DEAD_LETTERED = "DEAD_LETTERED"


class PipelineEvent(Base):
    __tablename__ = "pipeline_event"
    __table_args__ = (
        UniqueConstraint("source", "event_id", name="uq_pipeline_event_source_event_id"),
        Index("ix_pipeline_event_pipeline_occurred", "pipeline_id", "occurred_at"),
        Index("ix_pipeline_event_status", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(128))
    pipeline_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task.id"), index=True)
    event_type: Mapped[EventType] = mapped_column(str_enum(EventType, "event_type"))
    severity: Mapped[Severity] = mapped_column(str_enum(Severity, "event_severity"))
    occurred_at: Mapped[datetime] = mapped_column(tz())
    received_at: Mapped[datetime] = mapped_column(tz(), default=utcnow)
    correlation_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(100))
    event_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    execution: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    log_excerpt: Mapped[str | None] = mapped_column(Text)
    log_ref: Mapped[str | None] = mapped_column(String(512))
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[ProcessingStatus] = mapped_column(
        str_enum(ProcessingStatus, "processing_status"), default=ProcessingStatus.RECEIVED
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(tz())
    incident_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("incident.id"), index=True)
