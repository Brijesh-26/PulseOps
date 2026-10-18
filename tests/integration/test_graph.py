from __future__ import annotations

import pytest

from pulseops.errors import ValidationFailedError
from pulseops.pipelines.graph import add_dependency, downstream_impact, upstream_lineage
from pulseops.pipelines.repository import get_task_by_qualified_key
from simulator.topology import apply_topology
from tests.conftest import Ctx

DIAMOND = {
    "pipelines": [
        {
            "key": "p",
            "tasks": [
                {"key": "a", "produces": ["ds.a"]},
                {"key": "b", "produces": ["ds.b"]},
                {"key": "c", "produces": ["ds.c"]},
                {"key": "d", "produces": ["ds.d1", "ds.d2"]},
                {"key": "isolated"},
            ],
        }
    ],
    # a -> b -> d, a -> c -> d (diamond), d reachable by two paths
    "dependencies": [["p.a", "p.b"], ["p.a", "p.c"], ["p.b", "p.d"], ["p.c", "p.d"]],
}


@pytest.fixture
def diamond(ctx: Ctx) -> Ctx:
    with ctx.session() as s:
        apply_topology(s, DIAMOND)
        s.commit()
    return ctx


def _t(s, key):  # type: ignore[no-untyped-def]
    task = get_task_by_qualified_key(s, key)
    assert task is not None
    return task


def test_downstream_impact_of_diamond(diamond: Ctx) -> None:
    with diamond.session() as s:
        report = downstream_impact(s, _t(s, "p.a").id)
    assert report.origin == "p.a"
    assert report.task_keys == ["p.b", "p.c", "p.d"]  # d listed once despite two paths
    d = report.tasks[-1]
    assert d.depth == 2
    assert d.path in (["p.a", "p.b", "p.d"], ["p.a", "p.c", "p.d"])
    assert d.datasets == ["ds.d1", "ds.d2"]
    assert report.datasets == ["ds.a", "ds.b", "ds.c", "ds.d1", "ds.d2"]


def test_leaf_and_isolated_have_no_downstream(diamond: Ctx) -> None:
    with diamond.session() as s:
        assert downstream_impact(s, _t(s, "p.d").id).tasks == []
        iso = downstream_impact(s, _t(s, "p.isolated").id)
        assert iso.tasks == [] and iso.datasets == []


def test_upstream_lineage(diamond: Ctx) -> None:
    with diamond.session() as s:
        assert upstream_lineage(s, _t(s, "p.d").id).task_keys == ["p.b", "p.c", "p.a"]


def test_cycle_rejected(diamond: Ctx) -> None:
    with diamond.session() as s:
        with pytest.raises(ValidationFailedError) as exc:
            add_dependency(s, _t(s, "p.d"), _t(s, "p.a"))
        assert exc.value.code == "dependency_cycle"
        with pytest.raises(ValidationFailedError):
            add_dependency(s, _t(s, "p.a"), _t(s, "p.a"))


def test_duplicate_edge_is_idempotent(diamond: Ctx) -> None:
    with diamond.session() as s:
        e1 = add_dependency(s, _t(s, "p.a"), _t(s, "p.b"))
        e2 = add_dependency(s, _t(s, "p.a"), _t(s, "p.b"))
        assert e1.id == e2.id


def test_traversal_survives_cycle_inserted_out_of_band(diamond: Ctx) -> None:
    from sqlalchemy import text  # noqa: PLC0415

    with diamond.session() as s:
        a, d = _t(s, "p.a"), _t(s, "p.d")
        s.execute(
            text(
                "INSERT INTO pipeline_dependency"
                " (id, upstream_task_id, downstream_task_id, created_at)"
                " VALUES (gen_random_uuid(), :u, :d, now())"
            ),
            {"u": d.id, "d": a.id},
        )
        report = downstream_impact(s, a.id)
        # Terminates, and the visited-path guard never reports the origin as its own impact.
        assert report.task_keys == ["p.b", "p.c", "p.d"]
        s.rollback()
