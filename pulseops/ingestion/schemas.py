"""The event contract accepted by ``POST /api/v1/events``."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from pulseops.ingestion.models import EventType, ProcessingStatus, Severity

KEY_PATTERN = r"^[a-z0-9][a-z0-9_\-]{0,99}$"
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,127}$"

MAX_METADATA_BYTES = 8 * 1024
MAX_METADATA_KEYS = 50
MAX_LOG_EXCERPT_CHARS = 8000
MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_EVENT_AGE = timedelta(days=30)

Key = Annotated[str, Field(pattern=KEY_PATTERN)]

TASK_SCOPED_TYPES = frozenset(EventType)


class ExecutionInfo(BaseModel):
    """Optional execution details reported by the orchestrator."""

    model_config = ConfigDict(extra="forbid")

    run_id: Annotated[str, Field(pattern=ID_PATTERN)]
    attempt: Annotated[int, Field(ge=1, le=100)] = 1
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    duration_seconds: Annotated[float, Field(ge=0, le=7 * 24 * 3600)] | None = None
    error_type: Annotated[str, Field(max_length=100)] | None = None

    @model_validator(mode="after")
    def _ordering(self) -> ExecutionInfo:
        if self.finished_at and self.finished_at < self.started_at:
            raise ValueError("finished_at must not be before started_at")
        return self


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: Annotated[str, Field(pattern=ID_PATTERN, description="Producer-unique event id")]
    source: Annotated[str, Field(pattern=KEY_PATTERN, description="Producing system")]
    pipeline_id: Key
    task_id: Key | None = None
    event_type: EventType
    severity: Severity
    occurred_at: AwareDatetime
    correlation_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9\-_.]{8,64}$")] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    log_excerpt: Annotated[str, Field(max_length=MAX_LOG_EXCERPT_CHARS)] | None = None
    log_ref: Annotated[str, Field(max_length=512)] | None = None
    execution: ExecutionInfo | None = None

    @field_validator("metadata")
    @classmethod
    def _metadata_limits(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > MAX_METADATA_KEYS:
            raise ValueError(f"metadata may have at most {MAX_METADATA_KEYS} keys")
        if len(json.dumps(value, default=str)) > MAX_METADATA_BYTES:
            raise ValueError(f"metadata must serialise to at most {MAX_METADATA_BYTES} bytes")
        return value

    @field_validator("log_excerpt")
    @classmethod
    def _strip_nul(cls, value: str | None) -> str | None:
        # NUL bytes are rejected by Postgres text columns.
        return value.replace("\x00", "") if value else value

    @model_validator(mode="after")
    def _semantic_checks(self) -> EventIn:
        now = datetime.now(UTC)
        if self.occurred_at > now + MAX_CLOCK_SKEW:
            raise ValueError("occurred_at is in the future")
        if self.occurred_at < now - MAX_EVENT_AGE:
            raise ValueError("occurred_at is older than the 30 day ingestion window")
        if self.event_type in TASK_SCOPED_TYPES and not self.task_id:
            raise ValueError(f"task_id is required for {self.event_type}")
        if self.event_type == EventType.DEPENDENCY_FAILED:
            upstream = self.metadata.get("upstream_task")
            if not isinstance(upstream, str) or upstream.count(".") != 1:
                raise ValueError(
                    "DEPENDENCY_FAILED requires metadata.upstream_task as '<pipeline>.<task>'"
                )
        return self

    def payload_for_hash(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"correlation_id"})


class EventAccepted(BaseModel):
    id: uuid.UUID
    event_id: str
    source: str
    status: ProcessingStatus
    duplicate: bool
    received_at: datetime
    correlation_id: str
    incident_id: uuid.UUID | None = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: str
    source: str
    event_type: EventType
    severity: Severity
    occurred_at: datetime
    received_at: datetime
    correlation_id: str
    run_id: str | None
    status: ProcessingStatus
    attempts: int
    last_error: str | None
    incident_id: uuid.UUID | None
    log_excerpt: str | None
    metadata: dict[str, Any] = Field(validation_alias="event_metadata")
