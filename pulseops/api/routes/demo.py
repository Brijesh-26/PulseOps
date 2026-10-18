"""Demo-only scenario loader. Disabled unless PULSEOPS_DEMO_MODE=true; admin role required."""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from pulseops.api.deps import DbSession, get_correlation_id, get_runtime, require
from pulseops.audit import service as audit
from pulseops.audit.models import ActorType
from pulseops.auth.principal import Permission, Principal
from pulseops.errors import NotFoundError, PermissionDeniedError
from pulseops.ingestion.schemas import EventIn
from pulseops.ingestion.service import ingest_event
from pulseops.runtime import Runtime

router = APIRouter(prefix="/api/v1/demo", tags=["demo"])


class ScenarioInfo(BaseModel):
    key: str
    title: str
    description: str
    expected: dict[str, Any]


class ScenarioLoadResult(BaseModel):
    scenario: str
    tag: str
    deliveries: int
    created_events: int
    duplicate_deliveries: int
    event_ids: list[str]


def _demo_enabled(runtime: Annotated[Runtime, Depends(get_runtime)]) -> None:
    if not runtime.settings.demo_mode:
        # Behave as if the endpoint does not exist outside demo mode.
        raise NotFoundError("Not found")


Admin = Annotated[Principal, Depends(require(Permission.DEMO_ADMIN))]


@router.get(
    "/scenarios",
    response_model=list[ScenarioInfo],
    dependencies=[Depends(_demo_enabled)],
)
def list_demo_scenarios(_: Admin) -> list[ScenarioInfo]:
    from simulator.loader import list_scenarios  # noqa: PLC0415

    return [
        ScenarioInfo(key=s.key, title=s.title, description=s.description, expected=s.expected)
        for s in list_scenarios()
    ]


@router.post(
    "/scenarios/{key}",
    status_code=202,
    response_model=ScenarioLoadResult,
    dependencies=[Depends(_demo_enabled)],
    summary="Seed a scenario and deliver its events through the normal ingestion path",
)
def load_demo_scenario(
    key: str,
    session: DbSession,
    principal: Admin,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> ScenarioLoadResult:
    from simulator.loader import get_scenario, seed_scenario  # noqa: PLC0415

    if runtime.settings.environment == "prod":  # defence in depth; settings also forbid it
        raise PermissionDeniedError("Demo scenarios are disabled in prod")
    try:
        scenario = get_scenario(key)
    except KeyError as exc:
        raise NotFoundError(f"Unknown scenario {key!r}") from exc
    tag = secrets.token_hex(3)
    payloads = seed_scenario(session, scenario, tag=tag)
    created = 0
    ids: list[str] = []
    for payload in payloads:
        result = ingest_event(session, EventIn.model_validate(payload), correlation_id)
        created += int(result.created)
        ids.append(str(result.event.id))
    audit.record(
        session,
        actor=principal.name,
        actor_type=ActorType.USER,
        action="demo.scenario_loaded",
        entity_type="scenario",
        entity_id=scenario.key,
        details={"tag": tag, "deliveries": len(payloads), "created": created},
    )
    session.commit()
    return ScenarioLoadResult(
        scenario=scenario.key,
        tag=tag,
        deliveries=len(payloads),
        created_events=created,
        duplicate_deliveries=len(payloads) - created,
        event_ids=ids,
    )
