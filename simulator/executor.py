"""Simulated execution of allowlisted recovery actions.

Nothing here touches real infrastructure: actions only read and write the
simulator tables (``sim_resource``, ``sim_fixture``), simulated task
executions and dataset staleness flags inside PulseOps' own database. There
is no shell, network or subprocess access, and dispatch is a fixed mapping
from the ``ActionType`` enum to functions.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pulseops.evidence.service import add_dependency_evidence, add_evidence
from pulseops.incidents.models import EvidenceKind, Incident
from pulseops.pipelines.graph import upstream_lineage
from pulseops.pipelines.models import Dataset, ExecutionStatus, TaskExecution
from pulseops.pipelines.repository import get_task_by_qualified_key
from pulseops.remediation.models import ActionType, ProposalStatus, RecoveryProposal
from pulseops.remediation.port import Check, ExecutionResult
from simulator.models import SimFixture, SimResource

MAX_RECOVERY_RETRIES_PER_INCIDENT = 2


def _latest(session: Session, task_id: uuid.UUID) -> TaskExecution | None:
    return session.scalar(
        select(TaskExecution)
        .where(TaskExecution.task_id == task_id)
        .order_by(TaskExecution.started_at.desc(), TaskExecution.attempt.desc())
        .limit(1)
    )


class SimulatorSandbox:
    # ------------------------------------------------------------ preconditions

    def check_preconditions(
        self, session: Session, incident: Incident, action: ActionType, params: dict[str, Any]
    ) -> list[Check]:
        fn: dict[ActionType, Callable[[Session, Incident, dict[str, Any]], list[Check]]] = {
            ActionType.RETRY_TASK: self._pre_retry,
            ActionType.CHECK_DATABASE_CONNECTIVITY: self._pre_db,
            ActionType.REFRESH_DEPENDENCY_STATUS: self._pre_refresh,
            ActionType.MARK_DATASET_STALE: self._pre_stale,
            ActionType.REPROCESS_TEST_FIXTURE: self._pre_fixture,
        }
        return fn[action](session, incident, params)

    def _pre_retry(self, s: Session, incident: Incident, p: dict[str, Any]) -> list[Check]:
        task = get_task_by_qualified_key(s, p["task"])
        if task is None:
            return [Check("task_exists", False, f"{p['task']} not found")]
        checks = [Check("task_exists", True, p["task"])]
        latest = _latest(s, task.id)
        failed = latest is not None and latest.status in (
            ExecutionStatus.FAILED,
            ExecutionStatus.UPSTREAM_FAILED,
        )
        checks.append(
            Check(
                "latest_run_failed",
                failed,
                f"latest status {latest.status.value if latest else 'none'}",
            )
        )
        bad_upstream = []
        for up in upstream_lineage(s, task.id).tasks:
            up_latest = _latest(s, up.task_id)
            if up_latest is not None and up_latest.status != ExecutionStatus.SUCCEEDED:
                bad_upstream.append(f"{up.qualified_key}={up_latest.status.value}")
        checks.append(
            Check(
                "upstream_healthy", not bad_upstream, ", ".join(bad_upstream) or "all upstream ok"
            )
        )
        prior = s.scalar(
            select(func.count()).where(
                RecoveryProposal.incident_id == incident.id,
                RecoveryProposal.action_type == ActionType.RETRY_TASK,
                RecoveryProposal.status.in_([ProposalStatus.SUCCEEDED, ProposalStatus.FAILED]),
            )
        )
        checks.append(
            Check(
                "retry_budget",
                (prior or 0) < MAX_RECOVERY_RETRIES_PER_INCIDENT,
                f"at most {MAX_RECOVERY_RETRIES_PER_INCIDENT} recovery retries per incident",
            )
        )
        return checks

    def _pre_db(self, s: Session, incident: Incident, p: dict[str, Any]) -> list[Check]:
        res = s.get(SimResource, p["resource_key"])
        if res is None:
            return [Check("resource_exists", False, f"{p['resource_key']} not simulated")]
        return [
            Check("resource_exists", True, res.key),
            Check("resource_is_database", res.kind == "database", f"kind={res.kind}"),
        ]

    def _pre_refresh(self, s: Session, incident: Incident, p: dict[str, Any]) -> list[Check]:
        task = get_task_by_qualified_key(s, p["task"])
        return [Check("task_exists", task is not None, p["task"])]

    def _pre_stale(self, s: Session, incident: Incident, p: dict[str, Any]) -> list[Check]:
        rows = s.scalars(select(Dataset).where(Dataset.key.in_(p["datasets"]))).all()
        missing = sorted(set(p["datasets"]) - {d.key for d in rows})
        fresh = [d.key for d in rows if not d.is_stale]
        return [
            Check("datasets_exist", not missing, f"missing {missing}" if missing else "ok"),
            Check("not_already_stale", bool(fresh), f"{len(fresh)} dataset(s) to flag"),
        ]

    def _pre_fixture(self, s: Session, incident: Incident, p: dict[str, Any]) -> list[Check]:
        fx = s.get(SimFixture, p["fixture_key"])
        if fx is None:
            return [Check("fixture_exists", False, p["fixture_key"])]
        return [
            Check("fixture_exists", True, fx.key),
            Check("test_namespace", fx.key.startswith("test_"), "fixture keys must start test_"),
            Check("fixture_corrupt", fx.status == "CORRUPT", f"status={fx.status}"),
        ]

    # ---------------------------------------------------------------- execution

    def execute(
        self,
        session: Session,
        incident: Incident,
        action: ActionType,
        params: dict[str, Any],
        proposal_id: str,
    ) -> ExecutionResult:
        fn: dict[
            ActionType, Callable[[Session, Incident, dict[str, Any], str], ExecutionResult]
        ] = {
            ActionType.RETRY_TASK: self._retry,
            ActionType.CHECK_DATABASE_CONNECTIVITY: self._check_db,
            ActionType.REFRESH_DEPENDENCY_STATUS: self._refresh,
            ActionType.MARK_DATASET_STALE: self._stale,
            ActionType.REPROCESS_TEST_FIXTURE: self._fixture,
        }
        return fn[action](session, incident, params, proposal_id)

    def _retry(
        self, s: Session, incident: Incident, p: dict[str, Any], pid: str
    ) -> ExecutionResult:
        task = get_task_by_qualified_key(s, p["task"])
        assert task is not None  # guaranteed by preconditions
        latest = _latest(s, task.id)
        healthy = True
        if task.resource_key:
            res = s.get(SimResource, task.resource_key)
            healthy = res is None or res.healthy
        now = datetime.now(UTC)
        run_id = latest.run_id if latest else f"recovery-{pid[:8]}"
        attempt = (latest.attempt + 1) if latest else 1
        status = ExecutionStatus.SUCCEEDED if healthy else ExecutionStatus.FAILED
        s.add(
            TaskExecution(
                task_id=task.id,
                run_id=run_id,
                attempt=attempt,
                status=status,
                started_at=now,
                finished_at=now,
                duration_seconds=0.0,
                error_type=None if healthy else "OperationalError",
                error_message=None if healthy else f"{task.resource_key} unreachable (simulated)",
                triggered_by=f"recovery:{pid}",
            )
        )
        s.flush()
        after = _latest(s, task.id)
        post = after is not None and after.status == ExecutionStatus.SUCCEEDED
        return ExecutionResult(
            success=healthy,
            postcondition_ok=post,
            output={
                "task": p["task"],
                "run_id": run_id,
                "attempt": attempt,
                "status": status.value,
            },
            error=None if healthy else "Simulated retry failed: dependency still unhealthy",
        )

    def _check_db(
        self, s: Session, incident: Incident, p: dict[str, Any], pid: str
    ) -> ExecutionResult:
        res = s.get_one(SimResource, p["resource_key"])
        output = {"resource_key": res.key, "healthy": res.healthy, "latency_ms": res.latency_ms}
        ev = add_evidence(
            s,
            incident,
            kind=EvidenceKind.DIAGNOSTIC,
            source_ref=f"diagnostic:{pid}",
            title=f"Connectivity check of {res.key}: "
            + ("reachable" if res.healthy else "UNREACHABLE"),
            content=(
                f"Simulated probe of {res.key} at {datetime.now(UTC).isoformat()}: "
                f"healthy={res.healthy} latency_ms={res.latency_ms}"
            ),
            data={"healthy": res.healthy, "latency_ms": res.latency_ms, "resource": res.key},
            observed_at=datetime.now(UTC),
            collected_by="action:CHECK_DATABASE_CONNECTIVITY",
        )
        output["evidence_code"] = ev.code
        # The probe itself succeeds whether or not the database is healthy.
        return ExecutionResult(success=True, postcondition_ok=True, output=output)

    def _refresh(
        self, s: Session, incident: Incident, p: dict[str, Any], pid: str
    ) -> ExecutionResult:
        task = get_task_by_qualified_key(s, p["task"])
        assert task is not None
        items = add_dependency_evidence(s, incident, task.id)
        return ExecutionResult(
            success=True,
            postcondition_ok=True,
            output={"task": p["task"], "evidence_codes": [e.code for e in items]},
        )

    def _stale(
        self, s: Session, incident: Incident, p: dict[str, Any], pid: str
    ) -> ExecutionResult:
        now = datetime.now(UTC)
        rows = s.scalars(select(Dataset).where(Dataset.key.in_(p["datasets"]))).all()
        flagged = []
        for d in rows:
            if not d.is_stale:
                d.is_stale = True
                d.stale_since = now
                d.stale_reason = f"{incident.display_id}: upstream failure (proposal {pid})"
                flagged.append(d.key)
        s.flush()
        post = all(d.is_stale for d in rows) and len(rows) == len(p["datasets"])
        return ExecutionResult(
            success=True, postcondition_ok=post, output={"flagged": sorted(flagged)}
        )

    def _fixture(
        self, s: Session, incident: Incident, p: dict[str, Any], pid: str
    ) -> ExecutionResult:
        fx = s.get_one(SimFixture, p["fixture_key"])
        fx.checksum = fx.expected_checksum
        fx.status = "VALID"
        fx.reprocess_count += 1
        s.flush()
        post = fx.checksum == fx.expected_checksum
        return ExecutionResult(
            success=True,
            postcondition_ok=post,
            output={
                "fixture_key": fx.key,
                "checksum": fx.checksum,
                "reprocess_count": fx.reprocess_count,
            },
        )
