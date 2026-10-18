"""Interface between remediation and the execution sandbox (the simulator)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.orm import Session

from pulseops.incidents.models import Incident
from pulseops.remediation.models import ActionType


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class ExecutionResult:
    success: bool
    postcondition_ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class ExecutionSandbox(Protocol):
    def check_preconditions(
        self, session: Session, incident: Incident, action: ActionType, params: dict[str, Any]
    ) -> list[Check]: ...

    def execute(
        self,
        session: Session,
        incident: Incident,
        action: ActionType,
        params: dict[str, Any],
        proposal_id: str,
    ) -> ExecutionResult: ...
