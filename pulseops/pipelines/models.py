"""Pipeline topology and execution history."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pulseops.db.base import Base, created_at_col, str_enum, tz, uuid_pk


class ExecutionStatus(enum.StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UPSTREAM_FAILED = "UPSTREAM_FAILED"
    SKIPPED = "SKIPPED"


class Pipeline(Base):
    __tablename__ = "pipeline"

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_at_col()

    tasks: Mapped[list[Task]] = relationship(back_populates="pipeline", order_by="Task.key")


class Task(Base):
    __tablename__ = "task"
    __table_args__ = (UniqueConstraint("pipeline_id", "key", name="uq_task_pipeline_key"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    pipeline_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pipeline.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    # Simulated external resource the task depends on (e.g. a warehouse DB).
    resource_key: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_at_col()

    pipeline: Mapped[Pipeline] = relationship(back_populates="tasks")

    @property
    def qualified_key(self) -> str:
        return f"{self.pipeline.key}.{self.key}"


class Dataset(Base):
    __tablename__ = "dataset"

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(150), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    producer_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("task.id", ondelete="SET NULL"), index=True
    )
    is_stale: Mapped[bool] = mapped_column(default=False, server_default="false")
    stale_reason: Mapped[str | None] = mapped_column(Text)
    stale_since: Mapped[datetime | None] = mapped_column(tz())
    created_at: Mapped[datetime] = created_at_col()

    producer_task: Mapped[Task | None] = relationship()


class PipelineDependency(Base):
    """Directed edge: ``downstream_task`` consumes the output of ``upstream_task``.

    Edges are task-level and may cross pipeline boundaries. Cycles are rejected
    by the application on insert (see ``pipelines.graph``).
    """

    __tablename__ = "pipeline_dependency"
    __table_args__ = (
        UniqueConstraint("upstream_task_id", "downstream_task_id", name="uq_dependency_edge"),
        CheckConstraint("upstream_task_id <> downstream_task_id", name="no_self_loop"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    upstream_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"), index=True
    )
    downstream_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = created_at_col()

    upstream_task: Mapped[Task] = relationship(foreign_keys=[upstream_task_id])
    downstream_task: Mapped[Task] = relationship(foreign_keys=[downstream_task_id])


class TaskExecution(Base):
    __tablename__ = "task_execution"
    __table_args__ = (
        UniqueConstraint("task_id", "run_id", "attempt", name="uq_execution_run_attempt"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="finished_after_started"
        ),
        Index("ix_task_execution_task_started", "task_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task.id", ondelete="CASCADE"))
    run_id: Mapped[str] = mapped_column(String(100))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[ExecutionStatus] = mapped_column(str_enum(ExecutionStatus, "execution_status"))
    started_at: Mapped[datetime] = mapped_column(tz())
    finished_at: Mapped[datetime | None] = mapped_column(tz())
    duration_seconds: Mapped[float | None]
    error_type: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    triggered_by: Mapped[str] = mapped_column(String(100), default="scheduler")
    created_at: Mapped[datetime] = created_at_col()

    task: Mapped[Task] = relationship()


class LogRecord(Base):
    __tablename__ = "log_record"
    __table_args__ = (
        Index("ix_log_record_task_ts", "task_id", "ts"),
        Index("ix_log_record_search", "search", postgresql_using="gin"),
        CheckConstraint("char_length(message) <= 8192", name="message_length"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task.id", ondelete="CASCADE"))
    task_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("task_execution.id", ondelete="SET NULL"), index=True
    )
    ts: Mapped[datetime] = mapped_column(tz())
    level: Mapped[str] = mapped_column(String(10))
    message: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64), default="task")
    search: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', message)", persisted=True)
    )
