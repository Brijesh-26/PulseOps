"""Approval -> execution workflow, authorization and safety properties."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update

from pulseops.audit.service import verify_chain
from pulseops.auth.principal import Role
from pulseops.incidents.models import Incident, IncidentEvidence, IncidentStatus
from pulseops.messaging import outbox
from pulseops.messaging.queue import Envelope
from pulseops.pipelines.models import Dataset
from pulseops.remediation.models import RecoveryExecution, RecoveryProposal
from pulseops.workers.handlers import build_handlers
from pulseops.workers.worker import Worker
from simulator.evaluation import drain
from simulator.models import SimFixture, SimResource
from tests.conftest import Ctx


def _load(ctx: Ctx, key: str) -> dict:  # type: ignore[type-arg]
    resp = ctx.client.post(f"/api/v1/demo/scenarios/{key}", headers=ctx.headers(Role.ADMIN))
    assert resp.status_code == 202, resp.text
    drain(ctx.runtime)
    incidents = ctx.client.get("/api/v1/incidents", headers=ctx.headers(Role.VIEWER)).json()
    assert incidents["total"] == 1
    detail = ctx.client.get(
        f"/api/v1/incidents/{incidents['items'][0]['id']}", headers=ctx.headers(Role.VIEWER)
    )
    return detail.json()  # type: ignore[no-any-return]


def _approve(ctx: Ctx, proposal_id: str, role: Role = Role.APPROVER, key: str | None = None):  # type: ignore[no-untyped-def]
    headers = {"X-Api-Key": key} if key else ctx.headers(role)
    return ctx.client.post(
        f"/api/v1/proposals/{proposal_id}/approve", json={"reason": "looks right"}, headers=headers
    )


def test_scenario_a_full_lifecycle_to_resolution(ctx: Ctx) -> None:
    inc = _load(ctx, "a")
    assert inc["status"] == "ACTION_REQUIRED"
    first = inc["proposals"][0]
    assert first["action_type"] == "CHECK_DATABASE_CONNECTIVITY"
    assert first["proposed_by"] == "system:investigator"
    assert first["parameters"] == {"resource_key": "a-warehouse-db"}

    assert _approve(ctx, first["id"]).status_code == 200
    drain(ctx.runtime)
    inc = ctx.client.get(f"/api/v1/incidents/{inc['id']}", headers=ctx.headers(Role.VIEWER)).json()
    assert inc["status"] == "OPEN"  # diagnostic actions do not resolve
    executed = inc["proposals"][0]
    assert executed["status"] == "SUCCEEDED"
    assert executed["execution"]["output"]["healthy"] is True
    assert executed["decision"]["decided_by"] == "test-approver"

    resp = ctx.client.post(
        f"/api/v1/incidents/{inc['id']}/investigate", headers=ctx.headers(Role.OPERATOR)
    )
    assert resp.status_code == 202
    drain(ctx.runtime)
    inc = ctx.client.get(f"/api/v1/incidents/{inc['id']}", headers=ctx.headers(Role.VIEWER)).json()
    retry = inc["proposals"][0]
    assert retry["action_type"] == "RETRY_TASK"
    diag_codes = [
        e["code"]
        for e in ctx.client.get(
            f"/api/v1/incidents/{inc['id']}/evidence?kind=DIAGNOSTIC",
            headers=ctx.headers(Role.VIEWER),
        ).json()
    ]
    assert set(diag_codes) & set(retry["evidence_codes"])  # retry is justified by the probe

    assert _approve(ctx, retry["id"]).status_code == 200
    drain(ctx.runtime)
    inc = ctx.client.get(f"/api/v1/incidents/{inc['id']}", headers=ctx.headers(Role.VIEWER)).json()
    assert inc["status"] == "RESOLVED"
    assert inc["proposals"][0]["execution"]["postcondition_ok"] is True

    timeline = ctx.client.get(
        f"/api/v1/incidents/{inc['id']}/timeline", headers=ctx.headers(Role.VIEWER)
    ).json()
    actions = [t["title"] for t in timeline if t["kind"] == "audit"]
    for expected in (
        "incident.opened",
        "investigation.completed",
        "proposal.created",
        "proposal.approved",
        "execution.succeeded",
    ):
        assert expected in actions
    with ctx.session() as s:
        assert verify_chain(s).valid
    verify = ctx.client.get("/api/v1/audit/verify", headers=ctx.headers(Role.VIEWER)).json()
    assert verify["valid"] is True and verify["records_checked"] > 10


def test_reject_requires_reason_and_reopens(ctx: Ctx) -> None:
    inc = _load(ctx, "d")
    pid = inc["proposals"][0]["id"]
    bad = ctx.client.post(
        f"/api/v1/proposals/{pid}/reject", json={}, headers=ctx.headers(Role.APPROVER)
    )
    assert bad.status_code == 422
    ok = ctx.client.post(
        f"/api/v1/proposals/{pid}/reject",
        json={"reason": "fixture owner will fix manually"},
        headers=ctx.headers(Role.APPROVER),
    )
    assert ok.status_code == 200 and ok.json()["status"] == "REJECTED"
    drain(ctx.runtime)
    with ctx.session() as s:
        assert s.scalars(select(Incident)).one().status == IncidentStatus.OPEN
        assert s.scalar(select(func.count()).select_from(RecoveryExecution)) == 0


def test_authorization_on_decisions(ctx: Ctx) -> None:
    inc = _load(ctx, "d")
    pid = inc["proposals"][0]["id"]
    for role in (Role.VIEWER, Role.OPERATOR, Role.INGEST):
        assert _approve(ctx, pid, role).status_code == 403
    assert ctx.client.post(f"/api/v1/proposals/{pid}/approve").status_code == 401


def test_human_proposer_cannot_approve_own_proposal(ctx: Ctx) -> None:
    inc = _load(ctx, "d")
    # Reject the AI proposal so a human can propose instead.
    ctx.client.post(
        f"/api/v1/proposals/{inc['proposals'][0]['id']}/reject",
        json={"reason": "try human proposal"},
        headers=ctx.headers(Role.APPROVER),
    )
    evidence = ctx.client.get(
        f"/api/v1/incidents/{inc['id']}/evidence", headers=ctx.headers(Role.VIEWER)
    ).json()
    body = {
        "action_type": "REPROCESS_TEST_FIXTURE",
        "parameters": {"fixture_key": "test_orders_2026_10"},
        "rationale": "Fixture checksum mismatch",
        "evidence_codes": [evidence[0]["code"]],
    }
    created = ctx.client.post(
        f"/api/v1/incidents/{inc['id']}/proposals", json=body, headers=ctx.headers(Role.ADMIN)
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    own = _approve(ctx, pid, Role.ADMIN)
    assert own.status_code == 403
    assert own.json()["error"]["code"] == "self_approval_forbidden"
    assert _approve(ctx, pid, Role.APPROVER).status_code == 200


def test_proposal_validation_rejects_unsafe_requests(ctx: Ctx) -> None:
    inc = _load(ctx, "e")  # no AI proposal in this scenario; incident is OPEN
    assert inc["status"] == "OPEN" and inc["proposals"] == []
    url = f"/api/v1/incidents/{inc['id']}/proposals"
    h = ctx.headers(Role.OPERATOR)
    base = {"rationale": "operator judgement", "evidence_codes": ["E1"]}
    cases = [
        ({"action_type": "RUN_SHELL", "parameters": {"cmd": "rm -rf /"}}, "action_not_allowed"),
        (
            {"action_type": "RETRY_TASK", "parameters": {"task": "a_orders_etl.load_warehouse"}},
            "action_out_of_scope",
        ),
        (
            {"action_type": "RETRY_TASK", "parameters": {"task": "x.y", "shell": "true"}},
            "invalid_action_parameters",
        ),
        (
            {
                "action_type": "CHECK_DATABASE_CONNECTIVITY",
                "parameters": {"resource_key": "prod-db"},
            },
            "action_out_of_scope",
        ),
    ]
    for payload, code in cases:
        resp = ctx.client.post(url, json={**base, **payload}, headers=h)
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == code
    fake_evidence = ctx.client.post(
        url,
        json={
            "action_type": "RETRY_TASK",
            "parameters": {"task": "e_customer_sync.upsert_customers"},
            "rationale": "made up",
            "evidence_codes": ["E999"],
        },
        headers=h,
    )
    assert fake_evidence.status_code == 422
    assert fake_evidence.json()["error"]["code"] == "invalid_evidence"
    viewer = ctx.client.post(url, json={**base, **cases[1][0]}, headers=ctx.headers(Role.VIEWER))
    assert viewer.status_code == 403


def test_double_and_concurrent_approval_execute_once(ctx: Ctx) -> None:
    inc = _load(ctx, "d")
    pid = inc["proposals"][0]["id"]
    keys = [ctx.keys[Role.APPROVER], ctx.second_approver_key] * 3

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(lambda k: _approve(ctx, pid, key=k).status_code, keys))
    assert codes.count(200) == 1
    assert codes.count(409) == 5

    # Deliver the approval message again (at-least-once redelivery).
    ctx.queue.send(
        Envelope(message_id="dup", type=outbox.PROPOSAL_APPROVED, payload={"proposal_id": pid})
    )
    drain(ctx.runtime)
    ctx.queue.send(
        Envelope(message_id="dup2", type=outbox.PROPOSAL_APPROVED, payload={"proposal_id": pid})
    )
    drain(ctx.runtime)
    with ctx.session() as s:
        assert s.scalar(select(func.count()).select_from(RecoveryExecution)) == 1
        assert s.get_one(SimFixture, "test_orders_2026_10").reprocess_count == 1
        assert s.scalars(select(Incident)).one().status == IncidentStatus.RESOLVED


def test_stale_proposal_rejected_when_incident_changes(ctx: Ctx) -> None:
    inc = _load(ctx, "c")
    pid = inc["proposals"][0]["id"]
    # A new correlated downstream failure arrives after the proposal was made.
    event = {
        "event_id": "c-late-downstream",
        "source": "airflow",
        "pipeline_id": "c_finance",
        "task_id": "daily_revenue",
        "event_type": "DEPENDENCY_FAILED",
        "severity": "MEDIUM",
        "occurred_at": datetime.now(UTC).isoformat(),
        "metadata": {"upstream_task": "c_raw_ingest.extract_orders"},
    }
    assert (
        ctx.client.post("/api/v1/events", json=event, headers=ctx.headers(Role.INGEST)).status_code
        == 202
    )
    drain(ctx.runtime)
    resp = _approve(ctx, pid)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "stale_proposal"
    with ctx.session() as s:
        p = s.get_one(RecoveryProposal, pid)
        assert p.status.value == "STALE"
        assert s.scalars(select(Incident)).one().status == IncidentStatus.OPEN
        assert not any(d.is_stale for d in s.scalars(select(Dataset)))


def test_expired_proposal_cannot_be_approved(ctx: Ctx) -> None:
    inc = _load(ctx, "d")
    pid = inc["proposals"][0]["id"]
    with ctx.session() as s:
        s.execute(
            update(RecoveryProposal)
            .where(RecoveryProposal.id == pid)
            .values(
                created_at=datetime.now(UTC) - timedelta(hours=3),
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        s.commit()
    resp = _approve(ctx, pid)
    assert resp.status_code == 409 and "expired" in resp.json()["error"]["message"]


def test_precondition_failure_blocks_approval(ctx: Ctx) -> None:
    inc = _load(ctx, "d")
    with ctx.session() as s:
        s.get_one(SimFixture, "test_orders_2026_10").status = "VALID"  # fixed out of band
        s.commit()
    resp = _approve(ctx, inc["proposals"][0]["id"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "precondition_failed"


def test_preconditions_rechecked_at_execution_time(ctx: Ctx) -> None:
    inc = _load(ctx, "a")
    pid = inc["proposals"][0]["id"]
    assert _approve(ctx, pid).status_code == 200
    with ctx.session() as s:  # resource removed between approval and execution
        s.delete(s.get_one(SimResource, "a-warehouse-db"))
        s.commit()
    drain(ctx.runtime)
    with ctx.session() as s:
        ex = s.scalars(select(RecoveryExecution)).one()
        assert ex.status.value == "PRECONDITION_FAILED"
        assert s.get_one(RecoveryProposal, pid).status.value == "FAILED"
        assert s.scalars(select(Incident)).one().status == IncidentStatus.OPEN


def test_failed_simulated_retry_is_recorded_not_resolved(ctx: Ctx) -> None:
    inc = _load(ctx, "a")
    _approve(ctx, inc["proposals"][0]["id"])
    drain(ctx.runtime)
    ctx.client.post(
        f"/api/v1/incidents/{inc['id']}/investigate", headers=ctx.headers(Role.OPERATOR)
    )
    drain(ctx.runtime)
    retry = ctx.client.get(
        f"/api/v1/incidents/{inc['id']}", headers=ctx.headers(Role.VIEWER)
    ).json()["proposals"][0]
    with ctx.session() as s:  # database goes down again before the retry runs
        s.get_one(SimResource, "a-warehouse-db").healthy = False
        s.commit()
    _approve(ctx, retry["id"])
    drain(ctx.runtime)
    detail = ctx.client.get(
        f"/api/v1/incidents/{inc['id']}", headers=ctx.headers(Role.VIEWER)
    ).json()
    assert detail["status"] == "OPEN"
    assert detail["proposals"][0]["execution"]["status"] == "FAILED"
    assert detail["proposals"][0]["execution"]["postcondition_ok"] is False


def test_investigation_conflicts_and_manual_transitions(ctx: Ctx) -> None:
    inc = _load(ctx, "b")
    assert inc["status"] == "OPEN"
    url = f"/api/v1/incidents/{inc['id']}"
    h = ctx.headers(Role.OPERATOR)
    assert ctx.client.post(url + "/investigate", headers=h).status_code == 202
    second = ctx.client.post(url + "/investigate", headers=h)
    assert second.status_code == 409
    assert second.json()["error"]["code"] in ("investigation_in_progress", "invalid_state")
    drain(ctx.runtime)
    bad = ctx.client.post(
        url + "/transitions", json={"status": "RECOVERING", "reason": "force it"}, headers=h
    )
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "invalid_transition"
    ok = ctx.client.post(
        url + "/transitions", json={"status": "RESOLVED", "reason": "input volume spike"}, headers=h
    )
    assert ok.status_code == 200 and ok.json()["status"] == "RESOLVED"
    closed = ctx.client.post(
        url + "/transitions", json={"status": "CLOSED", "reason": "postmortem done"}, headers=h
    )
    assert closed.json()["status"] == "CLOSED"
    reopen = ctx.client.post(
        url + "/transitions", json={"status": "OPEN", "reason": "again"}, headers=h
    )
    assert reopen.status_code == 409


def test_incident_list_filters_and_search(ctx: Ctx) -> None:
    for key in ("b", "d"):
        ctx.client.post(f"/api/v1/demo/scenarios/{key}", headers=ctx.headers(Role.ADMIN))
    drain(ctx.runtime)
    h = ctx.headers(Role.VIEWER)
    all_ = ctx.client.get("/api/v1/incidents", headers=h).json()
    assert all_["total"] == 2
    lat = ctx.client.get("/api/v1/incidents?category=LATENCY_ANOMALY", headers=h).json()
    assert [i["category"] for i in lat["items"]] == ["LATENCY_ANOMALY"]
    sev = ctx.client.get("/api/v1/incidents?severity=LOW&severity=MEDIUM", headers=h).json()
    assert sev["total"] == 2
    q = ctx.client.get("/api/v1/incidents?q=fixtures", headers=h).json()
    assert q["total"] == 1 and q["items"][0]["pipeline"] == "d_fixtures"
    weird = ctx.client.get("/api/v1/incidents?q=%25'%20OR%201=1--", headers=h)
    assert weird.status_code == 200 and weird.json()["total"] == 0
    page = ctx.client.get("/api/v1/incidents?limit=1&offset=1&sort=-severity", headers=h).json()
    assert len(page["items"]) == 1 and page["total"] == 2
    assert ctx.client.get("/api/v1/incidents?limit=1000", headers=h).status_code == 422
    stats = ctx.client.get("/api/v1/stats", headers=h).json()
    assert stats["pending_approvals"] == 1 and stats["open_incidents"] == 2
    assert {p["pipeline"] for p in stats["pipelines"]} >= {"b_marketing", "d_fixtures"}


def test_evidence_search_and_impact_endpoint(ctx: Ctx) -> None:
    inc = _load(ctx, "c")
    h = ctx.headers(Role.VIEWER)
    found = ctx.client.get(f"/api/v1/incidents/{inc['id']}/evidence?q=503", headers=h).json()
    assert found and all("503" in e["content"] for e in found)
    impact = ctx.client.get(f"/api/v1/incidents/{inc['id']}/impact", headers=h).json()
    assert impact["origin"] == "c_raw_ingest.extract_orders"
    assert [t["task"] for t in impact["downstream"]][-1] == "c_finance.daily_revenue"
    assert impact["downstream"][-1]["path"][0] == "c_raw_ingest.extract_orders"
    with ctx.session() as s:
        assert s.scalar(select(func.count()).select_from(IncidentEvidence)) > 5


def test_worker_handlers_cover_all_message_types(ctx: Ctx) -> None:
    worker = Worker(ctx.queue, build_handlers(ctx.runtime), ctx.runtime.settings)
    assert set(worker.handlers) == {
        outbox.EVENT_INGESTED,
        outbox.INVESTIGATION_REQUESTED,
        outbox.PROPOSAL_APPROVED,
    }
