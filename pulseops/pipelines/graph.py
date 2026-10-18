"""Dependency graph operations: cycle-safe edge insertion and impact traversal.

Traversal uses recursive CTEs with an explicit visited-path guard and a depth
limit, so even a cycle introduced outside the application (e.g. manual SQL)
cannot cause unbounded recursion.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from pulseops.errors import ValidationFailedError
from pulseops.pipelines.models import Dataset, Pipeline, PipelineDependency, Task

MAX_DEPTH = 25
_GRAPH_LOCK_KEY = 0x47524150  # "GRAP": serialises concurrent edge inserts

_REACHABLE_SQL = text(
    """
    WITH RECURSIVE walk(task_id, depth, path) AS (
        SELECT d.downstream_task_id, 1, ARRAY[d.upstream_task_id, d.downstream_task_id]
        FROM pipeline_dependency d
        WHERE d.upstream_task_id = :start
      UNION ALL
        SELECT d.downstream_task_id, w.depth + 1, w.path || d.downstream_task_id
        FROM pipeline_dependency d
        JOIN walk w ON d.upstream_task_id = w.task_id
        WHERE NOT d.downstream_task_id = ANY(w.path) AND w.depth < :max_depth
    )
    SELECT task_id, depth, path FROM walk
    """
)

_UPSTREAM_SQL = text(
    """
    WITH RECURSIVE walk(task_id, depth, path) AS (
        SELECT d.upstream_task_id, 1, ARRAY[d.downstream_task_id, d.upstream_task_id]
        FROM pipeline_dependency d
        WHERE d.downstream_task_id = :start
      UNION ALL
        SELECT d.upstream_task_id, w.depth + 1, w.path || d.upstream_task_id
        FROM pipeline_dependency d
        JOIN walk w ON d.downstream_task_id = w.task_id
        WHERE NOT d.upstream_task_id = ANY(w.path) AND w.depth < :max_depth
    )
    SELECT task_id, depth, path FROM walk
    """
)


@dataclass(frozen=True)
class ImpactedTask:
    task_id: uuid.UUID
    qualified_key: str
    depth: int
    path: list[str]  # qualified keys from the origin task to this task
    datasets: list[str]


@dataclass(frozen=True)
class ImpactReport:
    origin: str
    tasks: list[ImpactedTask]
    datasets: list[str]  # all datasets produced by the origin or impacted tasks

    @property
    def task_keys(self) -> list[str]:
        return [t.qualified_key for t in self.tasks]


def _qualified_keys(session: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = session.execute(
        select(Task.id, Pipeline.key, Task.key).join(Pipeline).where(Task.id.in_(ids))
    ).all()
    return {tid: f"{p}.{t}" for tid, p, t in rows}


def _datasets_by_task(session: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    out: dict[uuid.UUID, list[str]] = {}
    if not ids:
        return out
    for producer, key in session.execute(
        select(Dataset.producer_task_id, Dataset.key)
        .where(Dataset.producer_task_id.in_(ids))
        .order_by(Dataset.key)
    ).all():
        if producer is not None:
            out.setdefault(producer, []).append(key)
    return out


def _walk(
    session: Session, sql: Any, start: uuid.UUID
) -> list[tuple[uuid.UUID, int, list[uuid.UUID]]]:
    rows = session.execute(sql, {"start": start, "max_depth": MAX_DEPTH}).all()
    return [(task_id, depth, list(path)) for task_id, depth, path in rows]


def is_reachable(session: Session, start: uuid.UUID, target: uuid.UUID) -> bool:
    return any(t == target for t, _, _ in _walk(session, _REACHABLE_SQL, start))


def add_dependency(session: Session, upstream: Task, downstream: Task) -> PipelineDependency:
    """Insert ``upstream -> downstream`` unless it would create a cycle."""
    if upstream.id == downstream.id:
        raise ValidationFailedError("A task cannot depend on itself", code="dependency_cycle")
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _GRAPH_LOCK_KEY})
    existing = session.scalar(
        select(PipelineDependency).where(
            PipelineDependency.upstream_task_id == upstream.id,
            PipelineDependency.downstream_task_id == downstream.id,
        )
    )
    if existing:
        return existing
    if is_reachable(session, downstream.id, upstream.id):
        raise ValidationFailedError(
            "Adding this dependency would create a cycle",
            code="dependency_cycle",
            details={"upstream": str(upstream.id), "downstream": str(downstream.id)},
        )
    edge = PipelineDependency(upstream_task_id=upstream.id, downstream_task_id=downstream.id)
    session.add(edge)
    session.flush()
    return edge


def _report(session: Session, origin: uuid.UUID, sql: Any) -> ImpactReport:
    walked = _walk(session, sql, origin)
    ids = {origin} | {n for _, _, p in walked for n in p}
    keys = _qualified_keys(session, ids)
    datasets = _datasets_by_task(session, ids)
    # Keep the shortest path per task; break ties by the lexicographically smallest path.
    best: dict[uuid.UUID, tuple[int, list[str]]] = {}
    for task_id, depth, path in walked:
        candidate = (depth, [keys[n] for n in path])
        if task_id not in best or candidate < best[task_id]:
            best[task_id] = candidate
    tasks = sorted(
        (
            ImpactedTask(
                task_id=t,
                qualified_key=keys[t],
                depth=d,
                path=p,
                datasets=datasets.get(t, []),
            )
            for t, (d, p) in best.items()
        ),
        key=lambda x: (x.depth, x.qualified_key),
    )
    all_datasets = sorted(
        {ds for t in [origin, *[x.task_id for x in tasks]] for ds in datasets.get(t, [])}
    )
    return ImpactReport(origin=keys[origin], tasks=tasks, datasets=all_datasets)


def downstream_impact(session: Session, task_id: uuid.UUID) -> ImpactReport:
    return _report(session, task_id, _REACHABLE_SQL)


def upstream_lineage(session: Session, task_id: uuid.UUID) -> ImpactReport:
    return _report(session, task_id, _UPSTREAM_SQL)
