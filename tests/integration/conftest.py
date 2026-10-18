from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from simulator.topology import apply_topology
from tests.conftest import Ctx

BASIC_TOPOLOGY: dict[str, Any] = {
    "pipelines": [
        {
            "key": "orders_etl",
            "name": "Orders ETL",
            "tasks": [
                {"key": "extract", "resource": "source-api", "produces": ["raw.orders"]},
                {"key": "load", "resource": "warehouse-db", "produces": ["dw.orders"]},
            ],
        },
        {
            "key": "reporting",
            "tasks": [{"key": "daily_revenue", "produces": ["mart.daily_revenue"]}],
        },
    ],
    "dependencies": [
        ["orders_etl.extract", "orders_etl.load"],
        ["orders_etl.load", "reporting.daily_revenue"],
    ],
}


@pytest.fixture
def topo(ctx: Ctx) -> Ctx:
    with ctx.session() as s:
        apply_topology(s, BASIC_TOPOLOGY)
        s.commit()
    return ctx


def make_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": "evt-0001",
        "source": "airflow",
        "pipeline_id": "orders_etl",
        "task_id": "load",
        "event_type": "TASK_FAILED",
        "severity": "HIGH",
        "occurred_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "metadata": {"dag_run": "scheduled__2026-10-18"},
        "log_excerpt": "psycopg.OperationalError: connection to warehouse-db timed out",
        "execution": {
            "run_id": "run-1",
            "attempt": 1,
            "started_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            "finished_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            "error_type": "OperationalError",
        },
    }
    event.update(overrides)
    return event
