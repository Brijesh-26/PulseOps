"""Lookups for pipeline topology."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from pulseops.errors import ValidationFailedError
from pulseops.pipelines.models import Pipeline, Task


def get_pipeline_by_key(session: Session, key: str) -> Pipeline | None:
    return session.scalar(select(Pipeline).where(Pipeline.key == key))


def get_task(session: Session, pipeline_id: uuid.UUID, key: str) -> Task | None:
    return session.scalar(
        select(Task)
        .options(joinedload(Task.pipeline))
        .where(Task.pipeline_id == pipeline_id, Task.key == key)
    )


def get_task_by_qualified_key(session: Session, qualified: str) -> Task | None:
    """Resolve ``"<pipeline>.<task>"``."""
    if qualified.count(".") != 1:
        return None
    pipeline_key, task_key = qualified.split(".")
    return session.scalar(
        select(Task)
        .join(Pipeline)
        .options(joinedload(Task.pipeline))
        .where(Pipeline.key == pipeline_key, Task.key == task_key)
    )


def resolve_pipeline_and_task(
    session: Session, pipeline_key: str, task_key: str | None
) -> tuple[Pipeline, Task | None]:
    pipeline = get_pipeline_by_key(session, pipeline_key)
    if pipeline is None:
        raise ValidationFailedError(f"Unknown pipeline {pipeline_key!r}", code="unknown_pipeline")
    task = None
    if task_key is not None:
        task = get_task(session, pipeline.id, task_key)
        if task is None:
            raise ValidationFailedError(
                f"Unknown task {task_key!r} in pipeline {pipeline_key!r}", code="unknown_task"
            )
    return pipeline, task
