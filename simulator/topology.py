"""Declarative topology loader: pipelines, tasks, datasets and dependency edges."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pulseops.pipelines.graph import add_dependency
from pulseops.pipelines.models import Dataset, Pipeline, Task
from pulseops.pipelines.repository import get_task_by_qualified_key


def apply_topology(session: Session, spec: dict[str, Any]) -> None:
    """Idempotently upsert a topology spec of the form::

    pipelines:
      - key: orders_etl
        name: Orders ETL
        tasks:
          - {key: extract, name: Extract, resource: warehouse-db, produces: [raw.orders]}
    dependencies:
      - [orders_etl.extract, orders_etl.transform]
    """
    for p in spec.get("pipelines", []):
        pipeline = session.scalar(select(Pipeline).where(Pipeline.key == p["key"]))
        if pipeline is None:
            pipeline = Pipeline(
                key=p["key"],
                name=p.get("name", p["key"]),
                description=p.get("description"),
                owner=p.get("owner"),
            )
            session.add(pipeline)
            session.flush()
        for t in p.get("tasks", []):
            task = session.scalar(
                select(Task).where(Task.pipeline_id == pipeline.id, Task.key == t["key"])
            )
            if task is None:
                task = Task(
                    pipeline_id=pipeline.id,
                    key=t["key"],
                    name=t.get("name", t["key"]),
                    resource_key=t.get("resource"),
                )
                session.add(task)
                session.flush()
            for ds_key in t.get("produces", []):
                if session.scalar(select(Dataset).where(Dataset.key == ds_key)) is None:
                    session.add(Dataset(key=ds_key, name=ds_key, producer_task_id=task.id))
    session.flush()
    for upstream_key, downstream_key in spec.get("dependencies", []):
        up = get_task_by_qualified_key(session, upstream_key)
        down = get_task_by_qualified_key(session, downstream_key)
        if up is None or down is None:
            raise ValueError(f"Unknown task in dependency {upstream_key} -> {downstream_key}")
        add_dependency(session, up, down)
    session.flush()
