"""Outbox relay + worker + incident engine (investigation messages not consumed here)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from pulseops.audit.models import AuditRecord
from pulseops.auth.principal import Role
from pulseops.incidents.models import Incident, IncidentCategory, IncidentEvidence, IncidentStatus
from pulseops.ingestion.models import PipelineEvent, ProcessingStatus
from pulseops.investigator.models import InvestigationRun, RunStatus
from pulseops.messaging import outbox
from pulseops.messaging.models import OutboxMessage
from pulseops.messaging.queue import Envelope
from pulseops.pipelines.models import ExecutionStatus, TaskExecution
from pulseops.workers.handlers import build_event_handler
from pulseops.workers.relay import OutboxRelay
from pulseops.workers.worker import Handler, Worker
from tests.conftest import Ctx, FakeClock
from tests.integration.conftest import make_event


def _worker(ctx: Ctx, extra: dict[str, Handler] | None = None) -> Worker:
    handlers = {outbox.EVENT_INGESTED: build_event_handler(ctx.runtime), **(extra or {})}
    return Worker(ctx.queue, handlers, ctx.runtime.settings)


def _relay(ctx: Ctx) -> OutboxRelay:
    return OutboxRelay(ctx.runtime.sessions, ctx.queue, ctx.runtime.settings)


def _post(ctx: Ctx, **kw: object) -> dict:  # type: ignore[type-arg]
    resp = ctx.client.post(
        "/api/v1/events", json=make_event(**kw), headers=ctx.headers(Role.INGEST)
    )
    assert resp.status_code in (200, 202), resp.text
    return resp.json()  # type: ignore[no-any-return]


def _drain_events_only(ctx: Ctx) -> None:
    """Relay everything, process event messages, leave other message types queued."""
    _relay(ctx).run_once()
    worker = _worker(ctx)
    for msg in ctx.queue.receive(max_messages=50):
        if msg.envelope.type == outbox.EVENT_INGESTED:
            worker.process(msg)


def test_event_flows_to_incident_with_evidence_and_auto_investigation(topo: Ctx) -> None:
    accepted = _post(topo)
    published = _relay(topo).run_once()
    assert published == 1
    assert _relay(topo).run_once() == 0  # nothing left to publish
    assert _worker(topo).run_once() == 1

    with topo.session() as s:
        ev = s.get_one(PipelineEvent, accepted["id"])
        assert ev.status == ProcessingStatus.PROCESSED
        incident = s.scalars(select(Incident)).one()
        assert ev.incident_id == incident.id
        assert incident.status == IncidentStatus.INVESTIGATING
        assert incident.category == IncidentCategory.TASK_FAILURE
        assert incident.fingerprint == "orders_etl.load:TASK_FAILURE"
        assert incident.event_count == 1
        codes = [e.code for e in s.scalars(select(IncidentEvidence)).all()]
        assert sorted(codes) == ["E1", "E2"]  # event + its log excerpt
        execution = s.scalars(select(TaskExecution)).one()
        assert execution.status == ExecutionStatus.FAILED
        run = s.scalars(select(InvestigationRun)).one()
        assert run.status == RunStatus.PENDING and run.trigger == "auto"
        pending = s.scalars(select(OutboxMessage).where(OutboxMessage.published_at.is_(None))).all()
        assert [m.message_type for m in pending] == [outbox.INVESTIGATION_REQUESTED]
        actions = [a.action for a in s.scalars(select(AuditRecord).order_by(AuditRecord.id))]
        assert actions == [
            "incident.opened",
            "incident.status_changed",
            "investigation.requested",
        ]


def test_redelivered_message_is_idempotent(topo: Ctx) -> None:
    accepted = _post(topo)
    worker = _worker(topo)
    env = Envelope(
        message_id="m1", type=outbox.EVENT_INGESTED, payload={"event_pk": accepted["id"]}
    )
    # The same message delivered three times (e.g. visibility timeout expiry, relay re-publish).
    for _ in range(3):
        topo.queue.send(env)
    assert worker.drain() == 3
    with topo.session() as s:
        assert s.scalar(select(func.count()).select_from(Incident)) == 1
        assert s.scalar(select(func.count()).select_from(InvestigationRun)) == 1
        assert s.scalars(select(Incident)).one().event_count == 1


def test_correlated_events_share_one_incident(topo: Ctx) -> None:
    _post(topo, event_id="e1", severity="MEDIUM")
    _post(topo, event_id="e2", severity="CRITICAL", execution=None)
    _drain_events_only(topo)
    with topo.session() as s:
        incident = s.scalars(select(Incident)).one()
        assert incident.event_count == 2
        assert incident.severity.value == "CRITICAL"  # escalates to max severity


def test_dependency_failure_groups_with_upstream_incident(topo: Ctx) -> None:
    _post(topo, event_id="up", task_id="extract", log_excerpt="HTTP 503 from source API")
    _post(
        topo,
        event_id="down",
        pipeline_id="reporting",
        task_id="daily_revenue",
        event_type="DEPENDENCY_FAILED",
        metadata={"upstream_task": "orders_etl.extract"},
        execution=None,
    )
    _drain_events_only(topo)
    with topo.session() as s:
        incident = s.scalars(select(Incident)).one()
        assert incident.fingerprint == "orders_etl.extract:TASK_FAILURE"
        assert incident.category == IncidentCategory.DEPENDENCY_FAILURE
        assert incident.event_count == 2


def test_success_event_does_not_open_incident(topo: Ctx) -> None:
    _post(topo, event_type="TASK_SUCCEEDED", severity="LOW", log_excerpt=None)
    _drain_events_only(topo)
    with topo.session() as s:
        assert s.scalar(select(func.count()).select_from(Incident)) == 0
        assert s.scalars(select(PipelineEvent)).one().status == ProcessingStatus.PROCESSED


def test_resolved_incident_is_not_reused(topo: Ctx) -> None:
    _post(topo, event_id="first")
    _drain_events_only(topo)
    with topo.session() as s:
        inc = s.scalars(select(Incident)).one()
        inc.status = IncidentStatus.RESOLVED
        s.commit()
    _post(topo, event_id="second")
    _drain_events_only(topo)
    with topo.session() as s:
        assert s.scalar(select(func.count()).select_from(Incident)) == 2


def test_transient_failure_retries_with_backoff_then_succeeds(topo: Ctx, clock: FakeClock) -> None:
    accepted = _post(topo)
    _relay(topo).run_once()
    real = build_event_handler(topo.runtime)
    calls = {"n": 0}

    def flaky(env: Envelope) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("database restarted")
        real.handle(env)

    worker = _worker(topo, {outbox.EVENT_INGESTED: Handler(flaky, real.on_failure)})
    assert worker.run_once() == 1
    with topo.session() as s:
        ev = s.get_one(PipelineEvent, accepted["id"])
        assert ev.status == ProcessingStatus.FAILED
        assert "database restarted" in (ev.last_error or "")
    assert worker.run_once() == 0  # invisible during backoff
    clock.advance(60)
    assert worker.run_once() == 1
    with topo.session() as s:
        ev = s.get_one(PipelineEvent, accepted["id"])
        assert ev.status == ProcessingStatus.PROCESSED
        assert ev.last_error is None
    assert topo.queue.dlq_depth == 0


def test_persistent_failure_is_dead_lettered_after_bounded_attempts(
    topo: Ctx, clock: FakeClock
) -> None:
    accepted = _post(topo)
    _relay(topo).run_once()
    real = build_event_handler(topo.runtime)

    def always_fails(env: Envelope) -> None:
        raise TimeoutError("still down")

    worker = _worker(topo, {outbox.EVENT_INGESTED: Handler(always_fails, real.on_failure)})
    attempts = 0
    for _ in range(10):
        attempts += worker.run_once()
        clock.advance(120)
    assert attempts == topo.runtime.settings.max_receive_count == 3
    assert topo.queue.depth == 0 and topo.queue.dlq_depth == 1
    assert topo.queue.dlq_reasons()[0].startswith("max attempts exhausted")
    with topo.session() as s:
        ev = s.get_one(PipelineEvent, accepted["id"])
        assert ev.status == ProcessingStatus.DEAD_LETTERED
        assert s.scalar(select(func.count()).select_from(Incident)) == 0  # rolled back


def test_poison_and_unknown_messages_are_dead_lettered_immediately(topo: Ctx) -> None:
    worker = _worker(topo)
    topo.queue.send(Envelope(message_id="x", type=outbox.EVENT_INGESTED, payload={}))
    topo.queue.send(Envelope(message_id="y", type="mystery.type", payload={}))
    worker.drain()
    assert topo.queue.dlq_depth == 2
    reasons = topo.queue.dlq_reasons()
    assert reasons[0] and reasons[0].startswith("permanent")
    assert reasons[1] and "unknown message type" in reasons[1]


def test_failed_publish_is_retried_by_relay(topo: Ctx) -> None:
    _post(topo)

    class BrokenQueue:
        def send(self, *_: object, **__: object) -> None:
            raise ConnectionError("queue unavailable")

    broken = OutboxRelay(topo.runtime.sessions, BrokenQueue(), topo.runtime.settings)  # type: ignore[arg-type]
    assert broken.run_once() == 0
    with topo.session() as s:
        row = s.scalars(select(OutboxMessage)).one()
        assert row.published_at is None and row.attempts == 1
        assert row.available_at > datetime.now(UTC)
    later = OutboxRelay(
        topo.runtime.sessions,
        topo.queue,
        topo.runtime.settings,
        now=lambda: datetime.now(UTC) + timedelta(minutes=10),
    )
    assert later.run_once() == 1
    assert topo.queue.depth == 1
