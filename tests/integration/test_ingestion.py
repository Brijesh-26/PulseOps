from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select

from pulseops.auth.principal import Role
from pulseops.ingestion.models import PipelineEvent
from pulseops.messaging.models import OutboxMessage
from tests.conftest import Ctx
from tests.integration.conftest import make_event


def _count(ctx: Ctx, model: type) -> int:
    with ctx.session() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


def test_event_is_persisted_with_outbox_message(topo: Ctx) -> None:
    resp = topo.client.post("/api/v1/events", json=make_event(), headers=topo.headers(Role.INGEST))
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["duplicate"] is False
    assert body["status"] == "RECEIVED"
    assert body["correlation_id"] == resp.headers["x-correlation-id"]
    with topo.session() as s:
        ev = s.scalars(select(PipelineEvent)).one()
        assert ev.event_type.value == "TASK_FAILED"
        assert ev.run_id == "run-1"
        msg = s.scalars(select(OutboxMessage)).one()
        assert msg.payload == {"event_pk": str(ev.id)}
        assert msg.published_at is None
        assert msg.correlation_id == ev.correlation_id


def test_duplicate_delivery_returns_original_without_new_outbox(topo: Ctx) -> None:
    h = topo.headers(Role.INGEST)
    event = make_event()
    first = topo.client.post("/api/v1/events", json=event, headers=h)
    second = topo.client.post("/api/v1/events", json=event, headers=h)
    third = topo.client.post("/api/v1/events", json=event, headers=h)
    assert first.status_code == 202
    assert second.status_code == 200 and third.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["id"] == first.json()["id"]
    assert _count(topo, PipelineEvent) == 1
    assert _count(topo, OutboxMessage) == 1


def test_concurrent_duplicate_delivery_is_safe(topo: Ctx) -> None:
    h = topo.headers(Role.INGEST)
    event = make_event()

    def send(_: int) -> int:
        return topo.client.post("/api/v1/events", json=event, headers=h).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(send, range(16)))
    assert codes.count(202) == 1
    assert codes.count(200) == 15
    assert _count(topo, PipelineEvent) == 1
    assert _count(topo, OutboxMessage) == 1


def test_event_id_reuse_with_different_payload_is_conflict(topo: Ctx) -> None:
    h = topo.headers(Role.INGEST)
    event = make_event()
    topo.client.post("/api/v1/events", json=event, headers=h)
    resp = topo.client.post("/api/v1/events", json={**event, "severity": "LOW"}, headers=h)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "event_id_reused"


def test_same_event_id_from_different_source_is_distinct(topo: Ctx) -> None:
    h = topo.headers(Role.INGEST)
    topo.client.post("/api/v1/events", json=make_event(), headers=h)
    resp = topo.client.post("/api/v1/events", json=make_event(source="dagster"), headers=h)
    assert resp.status_code == 202
    assert _count(topo, PipelineEvent) == 2


def test_unknown_pipeline_and_task_rejected(topo: Ctx) -> None:
    h = topo.headers(Role.INGEST)
    r1 = topo.client.post("/api/v1/events", json=make_event(pipeline_id="nope"), headers=h)
    r2 = topo.client.post("/api/v1/events", json=make_event(task_id="nope"), headers=h)
    assert r1.status_code == 422 and r1.json()["error"]["code"] == "unknown_pipeline"
    assert r2.status_code == 422 and r2.json()["error"]["code"] == "unknown_task"
    assert _count(topo, PipelineEvent) == 0


def test_malformed_events_rejected_with_error_schema(topo: Ctx) -> None:
    h = topo.headers(Role.INGEST)
    cases = [
        make_event(event_type="EXPLODED"),
        make_event(severity="APOCALYPTIC"),
        make_event(occurred_at="2026-10-18T10:00:00"),  # naive timestamp
        make_event(occurred_at="2099-01-01T00:00:00Z"),  # future
        make_event(unexpected_field=1),  # extra fields forbidden
        make_event(event_id="has spaces"),
        make_event(log_excerpt="x" * 9000),
        make_event(metadata={f"k{i}": i for i in range(51)}),
        make_event(task_id=None),
        make_event(event_type="DEPENDENCY_FAILED", metadata={}),
    ]
    for case in cases:
        resp = topo.client.post("/api/v1/events", json=case, headers=h)
        assert resp.status_code == 422, (case, resp.text)
        assert resp.json()["error"]["code"] == "validation_error"
    assert _count(topo, PipelineEvent) == 0


def test_oversized_body_rejected(topo: Ctx) -> None:
    resp = topo.client.post(
        "/api/v1/events",
        content=b'{"x":"' + b"a" * 70_000 + b'"}',
        headers={**topo.headers(Role.INGEST), "Content-Type": "application/json"},
    )
    assert resp.status_code == 413


def test_ingestion_requires_auth_and_permission(topo: Ctx) -> None:
    assert topo.client.post("/api/v1/events", json=make_event()).status_code == 401
    bad = topo.client.post(
        "/api/v1/events", json=make_event(), headers={"X-Api-Key": "po_live_" + "x" * 40}
    )
    assert bad.status_code == 401
    viewer = topo.client.post(
        "/api/v1/events", json=make_event(), headers=topo.headers(Role.VIEWER)
    )
    assert viewer.status_code == 403
    assert viewer.json()["error"]["code"] == "forbidden"


def test_list_events_paginates(topo: Ctx) -> None:
    h = topo.headers(Role.INGEST)
    for i in range(3):
        topo.client.post("/api/v1/events", json=make_event(event_id=f"e-{i}"), headers=h)
    resp = topo.client.get("/api/v1/events?limit=2", headers=topo.headers(Role.VIEWER))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3 and len(body["items"]) == 2
