"""Typed allowlist of recovery actions and their parameter validation.

Validation has two layers:
1. Structure: each action has a Pydantic parameter model (extra fields forbidden).
2. Scope: parameters must refer to things related to *this* incident: its task,
   that task's upstream/downstream dependencies, their resources and datasets.
   An investigator therefore cannot target unrelated infrastructure.

Preconditions against live (simulated) state are checked separately, at approval
and again immediately before execution (see ``simulator.executor``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from pulseops.errors import ValidationFailedError
from pulseops.incidents.models import Incident
from pulseops.pipelines.graph import downstream_impact, upstream_lineage
from pulseops.pipelines.models import Task
from pulseops.remediation.models import ActionType

QualifiedKey = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_\-]*\.[a-z0-9][a-z0-9_\-]*$")]


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryTaskParams(_Params):
    task: QualifiedKey


class CheckDatabaseParams(_Params):
    resource_key: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_\-]{0,99}$")]


class RefreshDependencyParams(_Params):
    task: QualifiedKey


class MarkDatasetStaleParams(_Params):
    datasets: Annotated[
        list[Annotated[str, Field(pattern=r"^[a-z0-9_.\-]{1,150}$")]],
        Field(min_length=1, max_length=20),
    ]


class ReprocessFixtureParams(_Params):
    fixture_key: Annotated[str, Field(pattern=r"^test_[a-z0-9_]{1,95}$")]


@dataclass(frozen=True)
class ActionSpec:
    action_type: ActionType
    params_model: type[_Params]
    description: str
    # A successful run of a resolving action moves the incident to RESOLVED;
    # otherwise the incident returns to OPEN with the result recorded as evidence.
    resolves_incident: bool


ACTIONS: dict[ActionType, ActionSpec] = {
    ActionType.RETRY_TASK: ActionSpec(
        ActionType.RETRY_TASK,
        RetryTaskParams,
        "Re-run a failed task once in the simulator (bounded; upstream must be healthy).",
        resolves_incident=True,
    ),
    ActionType.CHECK_DATABASE_CONNECTIVITY: ActionSpec(
        ActionType.CHECK_DATABASE_CONNECTIVITY,
        CheckDatabaseParams,
        "Read-only connectivity probe of a simulated database; result becomes evidence.",
        resolves_incident=False,
    ),
    ActionType.REFRESH_DEPENDENCY_STATUS: ActionSpec(
        ActionType.REFRESH_DEPENDENCY_STATUS,
        RefreshDependencyParams,
        "Re-read the latest status of a task's upstream dependencies; result becomes evidence.",
        resolves_incident=False,
    ),
    ActionType.MARK_DATASET_STALE: ActionSpec(
        ActionType.MARK_DATASET_STALE,
        MarkDatasetStaleParams,
        "Flag downstream datasets as stale so consumers do not trust them.",
        resolves_incident=False,
    ),
    ActionType.REPROCESS_TEST_FIXTURE: ActionSpec(
        ActionType.REPROCESS_TEST_FIXTURE,
        ReprocessFixtureParams,
        "Regenerate a corrupted test fixture batch (test_* namespace only).",
        resolves_incident=True,
    ),
}


@dataclass(frozen=True)
class ActionScope:
    """What an action for this incident may legitimately touch."""

    incident_task: str | None
    tasks: frozenset[str]
    resources: frozenset[str]
    datasets: frozenset[str]
    downstream_datasets: frozenset[str]


def build_scope(session: Session, incident: Incident) -> ActionScope:
    if incident.task_id is None:
        return ActionScope(None, frozenset(), frozenset(), frozenset(), frozenset())
    down = downstream_impact(session, incident.task_id)
    up = upstream_lineage(session, incident.task_id)
    tasks = {down.origin, *down.task_keys, *up.task_keys}
    ids = [incident.task_id, *[t.task_id for t in down.tasks], *[t.task_id for t in up.tasks]]
    resources = {
        t.resource_key
        for t in (session.get(Task, i) for i in ids)
        if t is not None and t.resource_key
    }
    return ActionScope(
        incident_task=down.origin,
        tasks=frozenset(tasks),
        resources=frozenset(resources),
        datasets=frozenset(down.datasets) | frozenset(up.datasets),
        downstream_datasets=frozenset(down.datasets),
    )


def validate_action(
    action_type: ActionType | str, parameters: dict[str, Any], scope: ActionScope
) -> dict[str, Any]:
    """Return normalised parameters or raise ValidationFailedError."""
    try:
        action = ActionType(action_type)
    except ValueError as exc:
        raise ValidationFailedError(
            f"Action {action_type!r} is not allowlisted", code="action_not_allowed"
        ) from exc
    spec = ACTIONS[action]
    clean = {k: v for k, v in parameters.items() if v is not None}
    try:
        params = spec.params_model.model_validate(clean)
    except ValidationError as exc:
        raise ValidationFailedError(
            f"Invalid parameters for {action.value}",
            code="invalid_action_parameters",
            details=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
        ) from exc

    def out_of_scope(what: str) -> ValidationFailedError:
        return ValidationFailedError(
            f"{action.value} {what} is outside this incident's scope",
            code="action_out_of_scope",
        )

    if isinstance(params, RetryTaskParams):
        if params.task != scope.incident_task:
            raise out_of_scope(f"task {params.task!r}")
    elif isinstance(params, RefreshDependencyParams):
        if params.task not in scope.tasks:
            raise out_of_scope(f"task {params.task!r}")
    elif isinstance(params, CheckDatabaseParams):
        if params.resource_key not in scope.resources:
            raise out_of_scope(f"resource {params.resource_key!r}")
    elif isinstance(params, MarkDatasetStaleParams):
        unknown = sorted(set(params.datasets) - scope.datasets)
        if unknown:
            raise out_of_scope(f"datasets {unknown}")
        params = MarkDatasetStaleParams(datasets=sorted(set(params.datasets)))
    return params.model_dump()
