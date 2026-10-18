"""Scenario loader: seeds a simulated orchestrator's state and produces events.

A scenario file describes topology, simulated resources/fixtures, historical
task executions, log lines and the events to deliver. History and logs are
written directly to the database, standing in for an orchestrator metadata
store (e.g. Airflow's) that PulseOps would read in production. Events are
returned as payloads for the ingestion API, so they go through validation,
deduplication, the outbox and the queue like any real event.

Determinism: all timestamps are relative to ``now`` and durations come from a
seeded RNG, so a scenario produces identical relative data on every run. A
``tag`` is appended to event and run IDs so a scenario can be loaded multiple
times in the same demo database without colliding; tests pass a fixed tag.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from pulseops.pipelines.models import Dataset, ExecutionStatus, LogRecord, TaskExecution
from pulseops.pipelines.repository import get_task_by_qualified_key
from simulator.models import SimFixture, SimResource
from simulator.topology import apply_topology

SCENARIO_DIR = Path(__file__).parent / "scenarios"


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    description: str
    spec: dict[str, Any]

    @property
    def expected(self) -> dict[str, Any]:
        return dict(self.spec.get("expected", {}))


def list_scenarios() -> list[Scenario]:
    return [load_file(p) for p in sorted(SCENARIO_DIR.glob("*.yaml"))]


def load_file(path: Path) -> Scenario:
    spec = yaml.safe_load(path.read_text())
    return Scenario(spec["key"], spec["title"], spec["description"].strip(), spec)


def get_scenario(key: str) -> Scenario:
    for s in list_scenarios():
        if s.key == key or s.key.split("_")[0] == key:
            return s
    raise KeyError(key)


def _tagged(value: str, tag: str) -> str:
    return f"{value}-{tag}" if tag else value


def seed_scenario(
    session: Session, scenario: Scenario, *, tag: str, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Seed state and return the event payloads to deliver (in delivery order)."""
    now = now or datetime.now(UTC)
    spec = scenario.spec
    rng = random.Random(spec.get("seed", 42))  # noqa: S311 - deterministic test data

    apply_topology(session, spec["topology"])

    for r in spec.get("resources", []):
        res = session.get(SimResource, r["key"]) or SimResource(key=r["key"])
        res.kind = r["kind"]
        res.healthy = r.get("healthy", True)
        res.latency_ms = r.get("latency_ms", 5)
        res.details = r.get("details", {})
        session.add(res)
    for f in spec.get("fixtures", []):
        fx = session.get(SimFixture, f["key"]) or SimFixture(key=f["key"], reprocess_count=0)
        fx.status = f["status"]
        fx.checksum = f["checksum"]
        fx.expected_checksum = f["expected_checksum"]
        session.add(fx)
    # Reset dataset freshness so reloading a scenario starts clean.
    for ds_key in spec.get("reset_datasets", []):
        ds = session.scalar(select(Dataset).where(Dataset.key == ds_key))
        if ds:
            ds.is_stale, ds.stale_reason, ds.stale_since = False, None, None
    session.flush()

    executions: dict[str, TaskExecution] = {}
    for h in spec.get("history", []):
        task = get_task_by_qualified_key(session, h["task"])
        if task is None:
            raise ValueError(f"history references unknown task {h['task']}")
        lo, hi = h.get("duration_seconds", [60, 120])
        for i in range(h.get("runs", 0)):
            started = now - timedelta(hours=h.get("interval_hours", 24) * (h["runs"] - i))
            dur = round(rng.uniform(lo, hi), 1)
            ex = TaskExecution(
                task_id=task.id,
                run_id=_tagged(f"hist-{i:03d}", tag),
                attempt=1,
                status=ExecutionStatus(h.get("status", "SUCCEEDED")),
                started_at=started,
                finished_at=started + timedelta(seconds=dur),
                duration_seconds=dur,
                triggered_by="scheduler",
            )
            session.add(ex)
        for cur in h.get("current", []):
            started = now + timedelta(minutes=cur["started_offset_minutes"])
            dur = cur.get("duration_seconds")
            ex = TaskExecution(
                task_id=task.id,
                run_id=_tagged(cur["run_id"], tag),
                attempt=cur.get("attempt", 1),
                status=ExecutionStatus(cur["status"]),
                started_at=started,
                finished_at=started + timedelta(seconds=dur) if dur is not None else None,
                duration_seconds=dur,
                error_type=cur.get("error_type"),
                error_message=cur.get("error_message"),
                triggered_by="scheduler",
            )
            session.add(ex)
            executions[f"{h['task']}:{cur['run_id']}"] = ex
    session.flush()

    for log in spec.get("logs", []):
        task = get_task_by_qualified_key(session, log["task"])
        if task is None:
            raise ValueError(f"log references unknown task {log['task']}")
        log_exec = executions.get(f"{log['task']}:{log.get('run_id')}")
        session.add(
            LogRecord(
                task_id=task.id,
                task_execution_id=log_exec.id if log_exec else None,
                ts=now + timedelta(minutes=log["offset_minutes"]),
                level=log["level"],
                message=log["message"].strip(),
                source="orchestrator",
            )
        )
    session.flush()

    events: list[dict[str, Any]] = []
    for ev in spec.get("events", []):
        pipeline_key, task_key = ev["task"].split(".")
        payload: dict[str, Any] = {
            "event_id": _tagged(ev["event_id"], tag),
            "source": ev.get("source", "airflow"),
            "pipeline_id": pipeline_key,
            "task_id": task_key,
            "event_type": ev["event_type"],
            "severity": ev["severity"],
            "occurred_at": (now + timedelta(minutes=ev["offset_minutes"])).isoformat(),
            "metadata": ev.get("metadata", {}),
        }
        if ev.get("log_excerpt"):
            payload["log_excerpt"] = ev["log_excerpt"].strip()
        if "execution" in ev:
            x = ev["execution"]
            started = now + timedelta(minutes=x["started_offset_minutes"])
            execution: dict[str, Any] = {
                "run_id": _tagged(x["run_id"], tag),
                "attempt": x.get("attempt", 1),
                "started_at": started.isoformat(),
            }
            if x.get("duration_seconds") is not None:
                execution["duration_seconds"] = x["duration_seconds"]
                execution["finished_at"] = (
                    started + timedelta(seconds=x["duration_seconds"])
                ).isoformat()
            if x.get("error_type"):
                execution["error_type"] = x["error_type"]
            payload["execution"] = execution
        for _ in range(ev.get("deliveries", 1)):
            events.append(payload)
    return events
