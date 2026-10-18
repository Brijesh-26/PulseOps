"""Recovery proposals, human approval decisions and simulated executions."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pulseops.db.base import Base, created_at_col, str_enum, tz, uuid_pk


class ActionType(enum.StrEnum):
    """The complete allowlist of actions. Nothing outside this enum can be proposed or run."""

    RETRY_TASK = "RETRY_TASK"
    CHECK_DATABASE_CONNECTIVITY = "CHECK_DATABASE_CONNECTIVITY"
    REFRESH_DEPENDENCY_STATUS = "REFRESH_DEPENDENCY_STATUS"
    MARK_DATASET_STALE = "MARK_DATASET_STALE"
    REPROCESS_TEST_FIXTURE = "REPROCESS_TEST_FIXTURE"


class ProposerType(enum.StrEnum):
    AI = "AI"
    HUMAN = "HUMAN"


class ProposalStatus(enum.StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STALE = "STALE"
    SUPERSEDED = "SUPERSEDED"


IN_FLIGHT_PROPOSAL_STATUSES = ("PENDING_APPROVAL", "APPROVED", "EXECUTING")


class Decision(enum.StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ExecutionOutcome(enum.StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"


class RecoveryProposal(Base):
    __tablename__ = "recovery_proposal"
    __table_args__ = (
        Index(
            "uq_recovery_proposal_in_flight",
            "incident_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING_APPROVAL','APPROVED','EXECUTING')"),
        ),
        Index("ix_recovery_proposal_incident_created", "incident_id", "created_at"),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident.id", ondelete="CASCADE"))
    investigation_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("investigation_run.id", ondelete="SET NULL")
    )
    action_type: Mapped[ActionType] = mapped_column(str_enum(ActionType, "action_type"))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    rationale: Mapped[str] = mapped_column(Text)
    evidence_codes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    proposed_by: Mapped[str] = mapped_column(String(100))
    proposer_type: Mapped[ProposerType] = mapped_column(str_enum(ProposerType, "proposer_type"))
    status: Mapped[ProposalStatus] = mapped_column(
        str_enum(ProposalStatus, "proposal_status"), default=ProposalStatus.PENDING_APPROVAL
    )
    incident_version: Mapped[int] = mapped_column(Integer)
    evidence_hash: Mapped[str] = mapped_column(String(64))
    status_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    expires_at: Mapped[datetime] = mapped_column(tz())
    updated_at: Mapped[datetime] = created_at_col()


class ApprovalDecision(Base):
    __tablename__ = "approval_decision"
    __table_args__ = (CheckConstraint("char_length(decided_by) > 0", name="decided_by_present"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    # Unique: a proposal can be decided exactly once.
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recovery_proposal.id", ondelete="CASCADE"), unique=True
    )
    decision: Mapped[Decision] = mapped_column(str_enum(Decision, "decision"))
    decided_by: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = created_at_col()


class RecoveryExecution(Base):
    __tablename__ = "recovery_execution"

    id: Mapped[uuid.UUID] = uuid_pk()
    # Unique: the database guarantees a proposal executes at most once.
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recovery_proposal.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[ExecutionOutcome] = mapped_column(
        str_enum(ExecutionOutcome, "execution_outcome"), default=ExecutionOutcome.RUNNING
    )
    started_at: Mapped[datetime] = created_at_col()
    finished_at: Mapped[datetime | None] = mapped_column(tz())
    preconditions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    postcondition_ok: Mapped[bool | None]
    error: Mapped[str | None] = mapped_column(Text)
