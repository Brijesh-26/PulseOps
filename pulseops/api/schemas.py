"""API response/request models (separate from ORM models and domain schemas)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pulseops.incidents.models import IncidentStatus


class _ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class IncidentSummary(BaseModel):
    id: uuid.UUID
    display_id: str
    title: str
    status: str
    severity: str
    category: str
    pipeline: str
    task: str | None
    opened_at: datetime
    updated_at: datetime
    last_event_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None
    event_count: int
    version: int
    investigation_status: str | None
    latest_confidence: float | None
    pending_proposal_id: uuid.UUID | None


class EvidenceOut(_ORM):
    id: uuid.UUID
    code: str
    kind: str
    title: str
    content: str
    data: dict[str, Any]
    observed_at: datetime | None
    collected_by: str
    collected_at: datetime


class HypothesisOut(BaseModel):
    rank: int
    title: str
    description: str
    category: str
    confidence: float
    is_supported: bool
    validation_notes: str | None
    supporting_evidence: list[str]
    contradicting_evidence: list[str]


class InvestigationOut(BaseModel):
    id: uuid.UUID
    status: str
    trigger: str
    requested_by: str
    provider: str
    model: str | None
    schema_version: str
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    error_message: str | None
    summary: str | None
    overall_confidence: float | None
    result: dict[str, Any] | None
    validation_issues: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    evidence_codes: list[str]
    hypotheses: list[HypothesisOut]


class DecisionOut(_ORM):
    decision: str
    decided_by: str
    decided_at: datetime
    reason: str | None


class ExecutionOut(_ORM):
    id: uuid.UUID
    status: str
    started_at: datetime
    finished_at: datetime | None
    preconditions: list[dict[str, Any]]
    output: dict[str, Any]
    postcondition_ok: bool | None
    error: str | None


class ProposalOut(BaseModel):
    id: uuid.UUID
    incident_id: uuid.UUID
    investigation_run_id: uuid.UUID | None
    action_type: str
    parameters: dict[str, Any]
    rationale: str
    evidence_codes: list[str]
    proposed_by: str
    proposer_type: str
    status: str
    status_reason: str | None
    incident_version: int
    created_at: datetime
    expires_at: datetime
    decision: DecisionOut | None
    execution: ExecutionOut | None


class IncidentDetail(IncidentSummary):
    fingerprint: str
    latest_investigation: InvestigationOut | None
    proposals: list[ProposalOut]


class TimelineEntry(BaseModel):
    at: datetime
    kind: str  # event | audit
    title: str
    actor: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ImpactTaskOut(BaseModel):
    task: str
    depth: int
    path: list[str]
    datasets: list[str]
    latest_status: str | None


class DatasetOut(BaseModel):
    key: str
    is_stale: bool
    stale_reason: str | None


class ImpactOut(BaseModel):
    origin: str | None
    downstream: list[ImpactTaskOut]
    upstream: list[ImpactTaskOut]
    datasets: list[DatasetOut]


class AuditOut(_ORM):
    id: int
    occurred_at: datetime
    actor: str
    actor_type: str
    action: str
    entity_type: str
    entity_id: str
    incident_id: uuid.UUID | None
    correlation_id: str | None
    details: dict[str, Any]
    hash: str


class InvestigateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=500)


class ProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_type: str = Field(max_length=64)
    parameters: dict[str, Any]
    rationale: str = Field(min_length=5, max_length=2000)
    evidence_codes: list[str] = Field(min_length=1, max_length=20)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=1000)


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=1000)


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: IncidentStatus
    reason: str = Field(min_length=3, max_length=500)
