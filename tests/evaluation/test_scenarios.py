"""Evaluation: every scenario end to end against known expected causes and evidence."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from pulseops.auth.principal import Role
from pulseops.incidents.models import Incident, IncidentStatus
from simulator.evaluation import drain, run_scenario, summarise
from simulator.loader import get_scenario, list_scenarios
from tests.conftest import Ctx

SCENARIOS = [s.key for s in list_scenarios()]


def test_five_scenarios_are_defined() -> None:
    assert len(SCENARIOS) >= 5
    assert {k[0] for k in SCENARIOS} >= {"a", "b", "c", "d", "e"}


@pytest.mark.parametrize("key", SCENARIOS)
def test_scenario_matches_expectations(ctx: Ctx, key: str) -> None:
    result = run_scenario(ctx.runtime, get_scenario(key), tag="eval")
    assert result.passed, result.as_dict()
    assert result.invalid_citations == 0


def test_duplicate_delivery_is_idempotent(ctx: Ctx) -> None:
    result = run_scenario(ctx.runtime, get_scenario("d"), tag="dup")
    assert result.deliveries == 3
    assert result.stored_events == 1
    assert result.incidents == 1


def test_uncertain_scenarios_propose_nothing(ctx: Ctx) -> None:
    for key in ("b", "e"):
        result = run_scenario(ctx.runtime, get_scenario(key), tag=f"u{key}")
        assert result.action_actual is None
        assert result.confidence is not None and result.confidence <= 0.4


def test_summary_metrics_are_computed_not_asserted(ctx: Ctx) -> None:
    results = [
        run_scenario(ctx.runtime, get_scenario(k), tag=f"s{i}") for i, k in enumerate(SCENARIOS)
    ]
    summary = summarise(results)
    assert summary["scenarios"] == len(SCENARIOS)
    assert summary["unjustified_actions"] == 0
    assert summary["invalid_citations_in_stored_results"] == 0


def test_demo_endpoint_loads_scenario_through_ingestion(ctx: Ctx) -> None:
    admin = ctx.headers(Role.ADMIN)
    listed = ctx.client.get("/api/v1/demo/scenarios", headers=admin)
    assert listed.status_code == 200 and len(listed.json()) >= 5
    resp = ctx.client.post("/api/v1/demo/scenarios/d", headers=admin)
    assert resp.status_code == 202, resp.text
    assert resp.json()["created_events"] == 1 and resp.json()["duplicate_deliveries"] == 2
    drain(ctx.runtime)
    with ctx.session() as s:
        assert s.scalars(select(Incident)).one().status == IncidentStatus.ACTION_REQUIRED


def test_demo_endpoint_requires_admin_and_demo_mode(ctx: Ctx) -> None:
    resp = ctx.client.post("/api/v1/demo/scenarios/a", headers=ctx.headers(Role.OPERATOR))
    assert resp.status_code == 403
    ctx.runtime.settings.demo_mode = False
    try:
        resp = ctx.client.post("/api/v1/demo/scenarios/a", headers=ctx.headers(Role.ADMIN))
        assert resp.status_code == 404
    finally:
        ctx.runtime.settings.demo_mode = True
