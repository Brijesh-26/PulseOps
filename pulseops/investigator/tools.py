"""Database-backed implementation of the investigator's retrieval tools.

Tools are scoped to the incident's own task and its dependency neighbourhood,
read-only with respect to pipeline state, size-limited, and every row they
return is persisted as incident evidence so it can be cited and audited.
Each call uses its own short transaction (no locks are held during LLM calls).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from pulseops.evidence import service as evidence
from pulseops.incidents.models import IncidentEvidence
from pulseops.incidents.service import get_incident
from pulseops.investigator.provider import EvidenceItem
from pulseops.pipelines.repository import get_task_by_qualified_key

MAX_QUERY_CHARS = 200


def to_item(ev: IncidentEvidence) -> EvidenceItem:
    return EvidenceItem(
        code=ev.code,
        kind=ev.kind.value,
        title=ev.title,
        content=ev.content,
        observed_at=ev.observed_at,
        data=ev.data,
    )


class ToolError(Exception):
    """Returned to the model as an error tool result; never fatal."""


class DbInvestigationTools:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        incident_id: uuid.UUID,
        related_tasks: list[str],
        max_calls: int,
    ) -> None:
        self.sessions = sessions
        self.incident_id = incident_id
        self.related_tasks = set(related_tasks)
        self.max_calls = max_calls
        self.calls: list[dict[str, Any]] = []
        self.presented_codes: set[str] = set()

    def _begin(self, name: str, args: dict[str, Any]) -> None:
        if len(self.calls) >= self.max_calls:
            raise ToolError(f"Tool call budget of {self.max_calls} exhausted")
        self.calls.append({"tool": name, "args": args})

    def _task(self, session: Session, key: str) -> Any:
        if key not in self.related_tasks:
            raise ToolError(
                f"Task {key!r} is not related to this incident. "
                f"Allowed: {sorted(self.related_tasks)}"
            )
        task = get_task_by_qualified_key(session, key)
        if task is None:
            raise ToolError(f"Unknown task {key!r}")
        return task

    def _finish(self, items: list[IncidentEvidence]) -> list[EvidenceItem]:
        out = [to_item(i) for i in items]
        self.presented_codes |= {i.code for i in out}
        self.calls[-1]["returned"] = [i.code for i in out]
        return out

    def search_logs(self, query: str) -> list[EvidenceItem]:
        query = query.strip()[:MAX_QUERY_CHARS]
        self._begin("search_logs", {"query": query})
        if not query:
            raise ToolError("query must not be empty")
        with self.sessions() as s, s.begin():
            incident = get_incident(s, self.incident_id)
            task_ids = [
                t.id
                for t in (get_task_by_qualified_key(s, k) for k in sorted(self.related_tasks))
                if t is not None
            ]
            since = incident.opened_at - timedelta(hours=24)
            logs = evidence.recent_logs(s, task_ids, since, query=query, limit=10)
            items = evidence.add_log_evidence(s, incident, logs, collected_by="tool:search_logs")
            return self._finish(items)

    def get_task_history(self, task: str) -> list[EvidenceItem]:
        self._begin("get_task_history", {"task": task})
        with self.sessions() as s, s.begin():
            t = self._task(s, task)
            incident = get_incident(s, self.incident_id)
            return self._finish(evidence.add_execution_evidence(s, incident, t.id))

    def get_dependencies(self, task: str) -> list[EvidenceItem]:
        self._begin("get_dependencies", {"task": task})
        with self.sessions() as s, s.begin():
            t = self._task(s, task)
            incident = get_incident(s, self.incident_id)
            return self._finish(evidence.add_dependency_evidence(s, incident, t.id))

    def get_baseline_durations(self, task: str) -> list[EvidenceItem]:
        self._begin("get_baseline_durations", {"task": task})
        with self.sessions() as s, s.begin():
            t = self._task(s, task)
            incident = get_incident(s, self.incident_id)
            ev = evidence.add_baseline_evidence(s, incident, t.id, task)
            return self._finish([ev] if ev else [])
