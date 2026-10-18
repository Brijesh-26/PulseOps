"""Versioned, strict schema for investigation results (v1).

Every claim must reference evidence codes (E1, E2…) that exist for the
incident. The backend validates codes, impact claims and proposed actions
after schema validation (see ``investigator.validation``).
"""

from __future__ import annotations

import enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from pulseops.remediation.models import ActionType

SCHEMA_VERSION = "1.0"

EvidenceCode = Annotated[str, Field(pattern=r"^E\d{1,4}$")]
Short = Annotated[str, Field(min_length=1, max_length=300)]
Medium = Annotated[str, Field(min_length=1, max_length=2000)]


class RootCauseCategory(enum.StrEnum):
    DATABASE_CONNECTIVITY = "DATABASE_CONNECTIVITY"
    UPSTREAM_DEPENDENCY_FAILURE = "UPSTREAM_DEPENDENCY_FAILURE"
    RESOURCE_EXHAUSTION = "RESOURCE_EXHAUSTION"
    DATA_QUALITY = "DATA_QUALITY"
    TEST_FIXTURE_CORRUPTION = "TEST_FIXTURE_CORRUPTION"
    CODE_OR_CONFIG_CHANGE = "CODE_OR_CONFIG_CHANGE"
    CAPACITY_OR_VOLUME = "CAPACITY_OR_VOLUME"
    TRANSIENT_INFRASTRUCTURE = "TRANSIENT_INFRASTRUCTURE"
    UNKNOWN = "UNKNOWN"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ObservedFact(_Strict):
    """Something directly visible in the evidence, not an inference."""

    statement: Short
    evidence_ids: Annotated[list[EvidenceCode], Field(min_length=1, max_length=10)]


class Hypothesis(_Strict):
    rank: Annotated[int, Field(ge=1, le=5)]
    title: Short
    category: RootCauseCategory
    reasoning: Medium = Field(description="Inference from the facts; label assumptions explicitly")
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    supporting_evidence_ids: Annotated[list[EvidenceCode], Field(max_length=15)]
    contradicting_evidence_ids: Annotated[list[EvidenceCode], Field(max_length=15)]


class ConfidenceAssessment(_Strict):
    overall: Annotated[float, Field(ge=0.0, le=1.0)]
    calibration: Literal["uncalibrated"] = Field(
        description="Scores are heuristic estimates, not calibrated probabilities"
    )
    rationale: Medium


class ImpactClaim(_Strict):
    affected_tasks: Annotated[list[Short], Field(max_length=50)]
    affected_datasets: Annotated[list[Short], Field(max_length=50)]
    explanation: Medium


class ActionParameters(_Strict):
    """Union of all allowlisted action parameters; validated per action by the backend."""

    task: Annotated[str, Field(max_length=201)] | None = None
    resource_key: Annotated[str, Field(max_length=100)] | None = None
    datasets: (
        Annotated[list[Annotated[str, Field(max_length=150)]], Field(max_length=20)] | None
    ) = None
    fixture_key: Annotated[str, Field(max_length=100)] | None = None


class ProposedAction(_Strict):
    action_type: ActionType
    parameters: ActionParameters
    hypothesis_rank: Annotated[int, Field(ge=1, le=5)]
    rationale: Medium
    supporting_evidence_ids: Annotated[list[EvidenceCode], Field(min_length=1, max_length=10)]


class InvestigationResultV1(_Strict):
    schema_version: Literal["1.0"]
    incident_id: Annotated[str, Field(pattern=r"^[0-9a-f\-]{36}$")]
    investigation_summary: Medium
    observed_facts: Annotated[list[ObservedFact], Field(max_length=25)]
    hypotheses: Annotated[list[Hypothesis], Field(max_length=5)]
    supporting_evidence_ids: Annotated[list[EvidenceCode], Field(max_length=25)] = Field(
        description="Evidence supporting the summary as a whole"
    )
    contradicting_evidence_ids: Annotated[list[EvidenceCode], Field(max_length=25)]
    confidence: ConfidenceAssessment
    uncertainty: Medium
    missing_information: Annotated[list[Short], Field(max_length=10)]
    downstream_impact: ImpactClaim
    recommended_diagnostics: Annotated[list[Short], Field(max_length=10)]
    proposed_action: ProposedAction | None
    suspicious_evidence_ids: Annotated[list[EvidenceCode], Field(max_length=25)] = Field(
        description="Evidence containing instruction-like or manipulative content"
    )
    explanation: Medium
