"""Investigation orchestration.

Phases (separate short transactions so no row locks are held during LLM calls):
1. claim:   PENDING -> RUNNING (idempotent; a non-PENDING run is skipped)
2. collect: gather the standard evidence set for the incident
3. investigate: call the provider (tools persist their own evidence)
4. persist: validate output, store result, hypotheses, evidence links,
   optional proposal, incident transition and audit records atomically.

Provider failures are recorded on the run as typed errors: retryable ones
(timeouts, rate limits, 5xx) return the run to PENDING and raise
RetryableError for the queue's backoff; others fail the run immediately.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from pulseops.audit import service as audit
from pulseops.audit.models import ActorType
from pulseops.auth.principal import AI_INVESTIGATOR_IDENTITY
from pulseops.errors import ConflictError, RetryableError
from pulseops.evidence.service import collect_for_incident, list_evidence
from pulseops.incidents.models import Incident, IncidentStatus
from pulseops.incidents.service import get_incident, transition
from pulseops.investigator.models import (
    EvidenceRelation,
    HypothesisEvidence,
    InvestigationEvidence,
    InvestigationRun,
    RootCauseHypothesis,
    RunStatus,
)
from pulseops.investigator.provider import (
    ImpactBrief,
    IncidentBrief,
    InvestigationContext,
    ProviderError,
)
from pulseops.investigator.safety import suspicious_codes
from pulseops.investigator.tools import DbInvestigationTools, to_item
from pulseops.investigator.validation import parse_output, validate_result
from pulseops.observability.logging import get_logger
from pulseops.observability.metrics import (
    INVESTIGATION_LATENCY,
    INVESTIGATIONS,
    UNSUPPORTED_CLAIMS,
)
from pulseops.pipelines.graph import downstream_impact
from pulseops.remediation.actions import ACTIONS, build_scope
from pulseops.remediation.models import ProposerType
from pulseops.remediation.service import create_proposal
from pulseops.runtime import Runtime

log = get_logger("pulseops.investigator")


def _brief(incident: Incident) -> IncidentBrief:
    return IncidentBrief(
        incident_id=str(incident.id),
        display_id=incident.display_id,
        title=incident.title,
        status=incident.status.value,
        severity=incident.severity.value,
        category=incident.category.value,
        pipeline=incident.pipeline.key,
        task=incident.task.qualified_key if incident.task else None,
        task_resource=incident.task.resource_key if incident.task else None,
        opened_at=incident.opened_at,
        event_count=incident.event_count,
    )


def _impact(session: object, incident: Incident) -> ImpactBrief:
    if incident.task_id is None:
        return ImpactBrief(origin=None, tasks=[], datasets=[])
    report = downstream_impact(session, incident.task_id)  # type: ignore[arg-type]
    return ImpactBrief(
        origin=report.origin,
        tasks=[
            {"task": t.qualified_key, "depth": t.depth, "path": t.path, "datasets": t.datasets}
            for t in report.tasks
        ],
        datasets=report.datasets,
    )


def _fail_run(runtime: Runtime, run_id: uuid.UUID, code: str, message: str, *, retry: bool) -> None:
    with runtime.sessions() as s, s.begin():
        run = s.get_one(InvestigationRun, run_id, with_for_update=True)
        run.error_code = code
        run.error_message = message[:2000]
        if retry:
            run.status = RunStatus.PENDING
            return
        run.status = RunStatus.FAILED
        run.finished_at = datetime.now(UTC)
        incident = get_incident(s, run.incident_id, lock=True)
        if incident.status == IncidentStatus.INVESTIGATING:
            transition(
                s,
                incident,
                IncidentStatus.OPEN,
                actor=AI_INVESTIGATOR_IDENTITY,
                actor_type=ActorType.AI,
                reason=f"Investigation failed: {code}",
            )
        audit.record(
            s,
            actor=AI_INVESTIGATOR_IDENTITY,
            actor_type=ActorType.AI,
            action="investigation.failed",
            entity_type="investigation_run",
            entity_id=run.id,
            incident_id=run.incident_id,
            details={"error_code": code, "message": message[:500]},
        )
    INVESTIGATIONS.labels(runtime.settings.investigator_provider, "failed").inc()


def fail_run_permanently(runtime: Runtime, run_id: uuid.UUID, message: str) -> None:
    with runtime.sessions() as s:
        run = s.get(InvestigationRun, run_id)
        if run is None or run.status in (RunStatus.SUCCEEDED, RunStatus.FAILED):
            return
    _fail_run(runtime, run_id, "retries_exhausted", message, retry=False)


def run_investigation(runtime: Runtime, run_id: uuid.UUID) -> InvestigationRun | None:
    settings = runtime.settings
    provider = runtime.provider

    # 1. claim
    with runtime.sessions() as s, s.begin():
        run = s.scalar(
            select(InvestigationRun).where(InvestigationRun.id == run_id).with_for_update()
        )
        if run is None or run.status != RunStatus.PENDING:
            return None
        run.status = RunStatus.RUNNING
        run.started_at = datetime.now(UTC)
        run.attempts += 1
        run.provider = provider.name
        incident_id = run.incident_id

    # 2. collect
    with runtime.sessions() as s, s.begin():
        incident = get_incident(s, incident_id, lock=True)
        evidence = collect_for_incident(s, incident)
        impact = _impact(s, incident)
        scope = build_scope(s, incident)
        context = InvestigationContext(
            incident=_brief(incident),
            evidence=[to_item(e) for e in evidence],
            impact=impact,
            allowed_actions=[
                {
                    "action_type": spec.action_type.value,
                    "description": spec.description,
                    "params": sorted(spec.params_model.model_fields),
                }
                for spec in ACTIONS.values()
            ],
            related_tasks=sorted(scope.tasks),
            min_action_confidence=settings.action_min_confidence,
        )

    # 3. investigate
    tools = DbInvestigationTools(
        runtime.sessions, incident_id, sorted(scope.tasks), settings.investigator_max_tool_calls
    )
    tools.presented_codes |= {e.code for e in context.evidence}
    started = time.perf_counter()
    try:
        result = provider.investigate(context, tools)
        parsed = parse_output(result.output, str(incident_id))
    except ProviderError as exc:
        retry = exc.retryable
        _fail_run(runtime, run_id, exc.code, str(exc), retry=retry)
        log.warning("investigation_provider_error", run_id=str(run_id), code=exc.code, retry=retry)
        if retry:
            raise RetryableError(f"{exc.code}: {exc}") from exc
        return None
    finally:
        INVESTIGATION_LATENCY.labels(provider.name).observe(time.perf_counter() - started)

    # 4. persist
    with runtime.sessions() as s, s.begin():
        run = s.get_one(InvestigationRun, run_id, with_for_update=True)
        incident = get_incident(s, incident_id, lock=True)
        all_evidence = {e.code: e for e in list_evidence(s, incident_id)}
        presented = {c for c in tools.presented_codes if c in all_evidence}
        validated = validate_result(
            parsed,
            known_codes=presented,
            impact=impact,
            scope=scope,
            min_action_confidence=settings.action_min_confidence,
            backend_suspicious=suspicious_codes(all_evidence[c] for c in presented),
        )
        UNSUPPORTED_CLAIMS.inc(validated.unsupported_claims)

        for code in sorted(presented):
            s.add(InvestigationEvidence(run_id=run.id, evidence_id=all_evidence[code].id))
        for hyp in validated.result.hypotheses:
            row = RootCauseHypothesis(
                run_id=run.id,
                incident_id=incident_id,
                rank=hyp.rank,
                title=hyp.title,
                description=hyp.reasoning,
                category=hyp.category.value,
                confidence=hyp.confidence,
                is_supported=bool(hyp.supporting_evidence_ids) or hyp.category.value == "UNKNOWN",
                validation_notes="; ".join(
                    i.message
                    for i in validated.issues
                    if i.path.startswith(f"hypotheses[{hyp.rank - 1}]")
                )
                or None,
            )
            s.add(row)
            s.flush()
            for relation, codes in (
                (EvidenceRelation.SUPPORTS, hyp.supporting_evidence_ids),
                (EvidenceRelation.CONTRADICTS, hyp.contradicting_evidence_ids),
            ):
                for code in codes:
                    s.add(
                        HypothesisEvidence(
                            hypothesis_id=row.id,
                            evidence_id=all_evidence[code].id,
                            relation=relation,
                        )
                    )

        run.status = RunStatus.SUCCEEDED
        run.finished_at = datetime.now(UTC)
        run.model = result.model
        run.result = validated.result.model_dump(mode="json")
        run.summary = validated.result.investigation_summary
        run.overall_confidence = validated.result.confidence.overall
        run.validation_issues = validated.issue_dicts
        run.tool_calls = result.tool_calls or tools.calls
        run.error_code = None
        run.error_message = None
        run.evidence_hash = hashlib.sha256(
            "|".join(f"{c}:{all_evidence[c].id}" for c in sorted(presented)).encode()
        ).hexdigest()
        s.flush()

        audit.record(
            s,
            actor=AI_INVESTIGATOR_IDENTITY,
            actor_type=ActorType.AI,
            action="investigation.completed",
            entity_type="investigation_run",
            entity_id=run.id,
            incident_id=incident_id,
            details={
                "provider": provider.name,
                "model": result.model,
                "hypotheses": len(validated.result.hypotheses),
                "overall_confidence": validated.result.confidence.overall,
                "validation_issues": len(validated.issues),
                "unsupported_claims": validated.unsupported_claims,
                "evidence_count": len(presented),
            },
        )

        proposal = None
        if validated.action is not None:
            try:
                with s.begin_nested():
                    proposal = create_proposal(
                        s,
                        incident,
                        action_type=validated.action["action_type"],
                        parameters=validated.action["parameters"],
                        rationale=validated.action["rationale"],
                        evidence_codes=validated.action["evidence_codes"],
                        proposed_by=AI_INVESTIGATOR_IDENTITY,
                        proposer_type=ProposerType.AI,
                        actor_type=ActorType.AI,
                        settings=settings,
                        investigation_run_id=run.id,
                    )
            except (ConflictError, ValueError) as exc:
                run.validation_issues = [
                    *run.validation_issues,
                    {"type": "proposal_rejected", "message": str(exc), "path": "proposed_action"},
                ]
                proposal = None
        if proposal is None and incident.status == IncidentStatus.INVESTIGATING:
            transition(
                s,
                incident,
                IncidentStatus.OPEN,
                actor=AI_INVESTIGATOR_IDENTITY,
                actor_type=ActorType.AI,
                reason="Investigation completed without a recommended action",
            )
    INVESTIGATIONS.labels(provider.name, "succeeded").inc()
    log.info(
        "investigation_completed",
        run_id=str(run_id),
        issues=len(validated.issues),
        proposed=validated.action is not None,
    )
    return run
