"""Scenario evaluation: run each scenario end to end and compare with expectations.

Metrics are computed from what the system actually stored, never asserted:
root-cause category, proposed action, evidence validity, unsupported claims
flagged by the validator, unjustified actions, confidence bounds, impact
correctness and duplicate handling.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, select

from pulseops.incidents.models import Incident, IncidentEvidence
from pulseops.ingestion.models import PipelineEvent
from pulseops.ingestion.schemas import EventIn
from pulseops.ingestion.service import ingest_event
from pulseops.investigator.models import InvestigationRun, RunStatus
from pulseops.pipelines.graph import downstream_impact
from pulseops.remediation.models import RecoveryProposal
from pulseops.runtime import Runtime
from pulseops.workers.handlers import build_handlers
from pulseops.workers.relay import OutboxRelay
from pulseops.workers.worker import Worker
from simulator.loader import Scenario


def drain(runtime: Runtime, rounds: int = 20) -> None:
    """Relay + worker until nothing moves (in-process, deterministic)."""
    relay = OutboxRelay(runtime.sessions, runtime.queue, runtime.settings)
    worker = Worker(runtime.queue, build_handlers(runtime), runtime.settings)
    for _ in range(rounds):
        moved = relay.run_once() + worker.drain()
        if moved == 0:
            return


@dataclass
class ScenarioResult:
    scenario: str
    provider: str
    incidents: int
    stored_events: int
    deliveries: int
    run_status: str | None
    root_cause_expected: str | None
    root_cause_actual: str | None
    root_cause_correct: bool | None
    action_expected: str | None
    action_actual: str | None
    action_correct: bool
    unjustified_action: bool
    confidence: float | None
    confidence_ok: bool
    cited_evidence: int
    invalid_citations: int
    unsupported_claims_flagged: int
    evidence_match_ok: bool
    impact_ok: bool
    missing_information_ok: bool
    suspicious_ok: bool
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(
            [
                self.root_cause_correct is not False,
                self.action_correct,
                not self.unjustified_action,
                self.confidence_ok,
                self.invalid_citations == 0,
                self.evidence_match_ok,
                self.impact_ok,
                self.missing_information_ok,
                self.suspicious_ok,
                self.incidents == 1,
            ]
        )

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "passed": self.passed}


def _cited(result: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    for f in result.get("observed_facts", []):
        codes += f["evidence_ids"]
    for h in result.get("hypotheses", []):
        codes += h["supporting_evidence_ids"] + h["contradicting_evidence_ids"]
    codes += result.get("supporting_evidence_ids", []) + result.get(
        "contradicting_evidence_ids", []
    )
    if result.get("proposed_action"):
        codes += result["proposed_action"]["supporting_evidence_ids"]
    return codes


def run_scenario(runtime: Runtime, scenario: Scenario, tag: str) -> ScenarioResult:
    from simulator.loader import seed_scenario  # noqa: PLC0415

    exp = scenario.expected
    with runtime.sessions() as s, s.begin():
        payloads = seed_scenario(s, scenario, tag=tag)
        for p in payloads:
            ingest_event(s, EventIn.model_validate(p), f"eval-{tag}-{scenario.key[:12]}")
    drain(runtime)

    notes: list[str] = []
    with runtime.sessions() as s:
        event_ids = {p["event_id"] for p in payloads}
        events = s.scalars(select(PipelineEvent).where(PipelineEvent.event_id.in_(event_ids))).all()
        incident_ids = {e.incident_id for e in events if e.incident_id}
        incidents = s.scalars(select(Incident).where(Incident.id.in_(incident_ids))).all()
        incident = incidents[0] if incidents else None
        run = (
            s.scalar(
                select(InvestigationRun)
                .where(InvestigationRun.incident_id == incident.id)
                .order_by(InvestigationRun.created_at)
                .limit(1)
            )
            if incident
            else None
        )
        result = (run.result or {}) if run else {}
        proposal = (
            s.scalar(
                select(RecoveryProposal)
                .where(RecoveryProposal.investigation_run_id == run.id)
                .limit(1)
            )
            if run
            else None
        )
        codes = (
            {
                c
                for (c,) in s.execute(
                    select(IncidentEvidence.code).where(IncidentEvidence.incident_id == incident.id)
                )
            }
            if incident
            else set()
        )
        cited = _cited(result)
        invalid = [c for c in cited if c not in codes]
        hyps = result.get("hypotheses", [])
        top = hyps[0]["category"] if hyps else None
        expected_cause = exp.get("root_cause_category")
        confidence = run.overall_confidence if run else None
        lo, hi = exp.get("min_confidence", 0.0), exp.get("max_confidence", 1.0)
        confidence_ok = confidence is not None and lo <= confidence <= hi
        if not confidence_ok:
            notes.append(f"confidence {confidence} outside [{lo}, {hi}]")
        action = proposal.action_type.value if proposal else None
        expected_action = exp.get("proposed_action")

        evidence_text = " ".join(
            e.content
            for e in s.scalars(
                select(IncidentEvidence).where(
                    IncidentEvidence.incident_id == (incident.id if incident else None),
                    IncidentEvidence.code.in_(cited or ["-"]),
                )
            )
        )
        match_ok = all(
            p.lower() in evidence_text.lower() for p in exp.get("evidence_must_match", [])
        )
        kinds_ok = True
        if exp.get("evidence_kinds") and incident:
            cited_kinds = {
                k.value
                for (k,) in s.execute(
                    select(IncidentEvidence.kind).where(
                        IncidentEvidence.incident_id == incident.id,
                        IncidentEvidence.code.in_(cited or ["-"]),
                    )
                )
            }
            kinds_ok = set(exp["evidence_kinds"]) <= cited_kinds
        if not (match_ok and kinds_ok):
            notes.append("expected evidence was not cited")

        impact_ok = True
        if incident and incident.task_id:
            report = downstream_impact(s, incident.task_id)
            impact_ok = report.task_keys == exp.get("impacted_tasks", report.task_keys) and (
                report.datasets == sorted(exp.get("impacted_datasets", report.datasets))
            )
            for task, path in (exp.get("impacted_paths") or {}).items():
                node = next((t for t in report.tasks if t.qualified_key == task), None)
                impact_ok = impact_ok and node is not None and node.path == path
            stored = result.get("downstream_impact", {})
            impact_ok = impact_ok and stored.get("affected_tasks") == sorted(report.task_keys)
        if not impact_ok:
            notes.append("dependency impact did not match expectation")

        missing_ok = not exp.get("requires_missing_information") or bool(
            result.get("missing_information")
        )
        suspicious_ok = not exp.get("requires_suspicious_evidence") or bool(
            result.get("suspicious_evidence_ids")
        )
        stored_events = s.scalar(select(func.count()).where(PipelineEvent.event_id.in_(event_ids)))
        unsupported = sum(
            1
            for i in (run.validation_issues if run else [])
            if i["type"].startswith(("unknown_evidence", "unsupported"))
        )

    return ScenarioResult(
        scenario=scenario.key,
        provider=runtime.provider.name,
        incidents=len(incidents),
        stored_events=stored_events or 0,
        deliveries=len(payloads),
        run_status=run.status.value if run else None,
        root_cause_expected=expected_cause,
        root_cause_actual=top,
        root_cause_correct=(top == expected_cause) if expected_cause else None,
        action_expected=expected_action,
        action_actual=action,
        action_correct=action == expected_action,
        unjustified_action=expected_action is None and action is not None,
        confidence=confidence,
        confidence_ok=confidence_ok and run is not None and run.status == RunStatus.SUCCEEDED,
        cited_evidence=len(cited),
        invalid_citations=len(invalid),
        unsupported_claims_flagged=unsupported,
        evidence_match_ok=match_ok and kinds_ok,
        impact_ok=impact_ok,
        missing_information_ok=missing_ok,
        suspicious_ok=suspicious_ok,
        notes=notes,
    )


def summarise(results: list[ScenarioResult]) -> dict[str, Any]:
    n = len(results)
    rc = [r for r in results if r.root_cause_correct is not None]
    return {
        "scenarios": n,
        "passed": sum(r.passed for r in results),
        "root_cause_accuracy": (sum(bool(r.root_cause_correct) for r in rc) / len(rc))
        if rc
        else None,
        "root_cause_scored_scenarios": len(rc),
        "action_accuracy": sum(r.action_correct for r in results) / n if n else None,
        "unjustified_actions": sum(r.unjustified_action for r in results),
        "invalid_citations_in_stored_results": sum(r.invalid_citations for r in results),
        "unsupported_claims_flagged_by_validator": sum(
            r.unsupported_claims_flagged for r in results
        ),
    }
