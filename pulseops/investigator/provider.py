"""Provider-neutral investigation interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel

from pulseops.config import Settings


class EvidenceItem(BaseModel):
    code: str
    kind: str
    title: str
    content: str
    observed_at: datetime | None = None
    data: dict[str, Any] = {}


class IncidentBrief(BaseModel):
    incident_id: str
    display_id: str
    title: str
    status: str
    severity: str
    category: str
    pipeline: str
    task: str | None
    task_resource: str | None
    opened_at: datetime
    event_count: int


class ImpactBrief(BaseModel):
    """Downstream impact computed deterministically by the backend (authoritative)."""

    origin: str | None
    tasks: list[dict[str, Any]]
    datasets: list[str]


class InvestigationContext(BaseModel):
    incident: IncidentBrief
    evidence: list[EvidenceItem]
    impact: ImpactBrief
    allowed_actions: list[dict[str, Any]]
    related_tasks: list[str]
    min_action_confidence: float


class InvestigationTools(Protocol):
    """Typed, read-only evidence retrieval. Every result is stored as evidence first."""

    def search_logs(self, query: str) -> list[EvidenceItem]: ...

    def get_task_history(self, task: str) -> list[EvidenceItem]: ...

    def get_dependencies(self, task: str) -> list[EvidenceItem]: ...

    def get_baseline_durations(self, task: str) -> list[EvidenceItem]: ...


@dataclass
class ProviderResult:
    output: dict[str, Any]
    model: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    code = "provider_error"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"
    retryable = True


class ProviderRateLimitedError(ProviderError):
    code = "provider_rate_limited"
    retryable = True


class ProviderUnavailableError(ProviderError):
    """Missing credentials, auth failure, or a transient outage (see ``retryable``)."""

    code = "provider_unavailable"


class MalformedOutputError(ProviderError):
    code = "malformed_output"


class ProviderRefusedError(ProviderError):
    code = "provider_refused"


class InvestigationProvider(Protocol):
    name: str

    def investigate(
        self, context: InvestigationContext, tools: InvestigationTools
    ) -> ProviderResult: ...


def build_provider(settings: Settings) -> InvestigationProvider:
    if settings.investigator_provider == "anthropic":
        from pulseops.investigator.anthropic_provider import AnthropicProvider  # noqa: PLC0415

        return AnthropicProvider(settings)
    from pulseops.investigator.mock_provider import MockProvider  # noqa: PLC0415

    return MockProvider()
