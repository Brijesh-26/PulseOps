"""Imports every model so metadata is complete (for Alembic and tests)."""

from pulseops.audit.models import AuditRecord
from pulseops.auth.models import ApiPrincipal
from pulseops.db.base import Base
from pulseops.incidents.models import Incident, IncidentEvidence
from pulseops.ingestion.models import PipelineEvent
from pulseops.investigator.models import (
    HypothesisEvidence,
    InvestigationEvidence,
    InvestigationRun,
    RootCauseHypothesis,
)
from pulseops.messaging.models import OutboxMessage
from pulseops.pipelines.models import (
    Dataset,
    LogRecord,
    Pipeline,
    PipelineDependency,
    Task,
    TaskExecution,
)
from pulseops.remediation.models import ApprovalDecision, RecoveryExecution, RecoveryProposal
from simulator.models import SimFixture, SimResource

__all__ = [
    "ApiPrincipal",
    "ApprovalDecision",
    "AuditRecord",
    "Base",
    "Dataset",
    "HypothesisEvidence",
    "Incident",
    "IncidentEvidence",
    "InvestigationEvidence",
    "InvestigationRun",
    "LogRecord",
    "OutboxMessage",
    "Pipeline",
    "PipelineDependency",
    "PipelineEvent",
    "RecoveryExecution",
    "RecoveryProposal",
    "RootCauseHypothesis",
    "SimFixture",
    "SimResource",
    "Task",
    "TaskExecution",
]
