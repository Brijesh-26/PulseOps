"""Investigation runs, the evidence they used, and their hypotheses."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pulseops.db.base import Base, created_at_col, str_enum, tz, uuid_pk


class RunStatus(enum.StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class EvidenceRelation(enum.StrEnum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"


class InvestigationRun(Base):
    __tablename__ = "investigation_run"
    __table_args__ = (
        Index(
            "uq_investigation_run_active",
            "incident_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING','RUNNING')"),
        ),
        Index("ix_investigation_run_incident_created", "incident_id", "created_at"),
        CheckConstraint(
            "overall_confidence IS NULL OR (overall_confidence >= 0 AND overall_confidence <= 1)",
            name="confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident.id", ondelete="CASCADE"))
    status: Mapped[RunStatus] = mapped_column(
        str_enum(RunStatus, "run_status"), default=RunStatus.PENDING
    )
    trigger: Mapped[str] = mapped_column(String(16))  # auto | manual
    requested_by: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(100))
    schema_version: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = created_at_col()
    started_at: Mapped[datetime | None] = mapped_column(tz())
    finished_at: Mapped[datetime | None] = mapped_column(tz())
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    overall_confidence: Mapped[float | None] = mapped_column(Float)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    validation_issues: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    evidence_hash: Mapped[str | None] = mapped_column(String(64))


class InvestigationEvidence(Base):
    """Exactly which evidence items were presented to the investigator for a run."""

    __tablename__ = "investigation_evidence"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigation_run.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incident_evidence.id", ondelete="CASCADE"), primary_key=True
    )


class RootCauseHypothesis(Base):
    __tablename__ = "root_cause_hypothesis"
    __table_args__ = (
        UniqueConstraint("run_id", "rank", name="uq_hypothesis_run_rank"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigation_run.id", ondelete="CASCADE"), index=True
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incident.id", ondelete="CASCADE"), index=True
    )
    rank: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float)
    is_supported: Mapped[bool] = mapped_column(default=True)
    validation_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


class HypothesisEvidence(Base):
    __tablename__ = "hypothesis_evidence"

    hypothesis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("root_cause_hypothesis.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incident_evidence.id", ondelete="CASCADE"), primary_key=True
    )
    relation: Mapped[EvidenceRelation] = mapped_column(
        str_enum(EvidenceRelation, "evidence_relation"), primary_key=True
    )
