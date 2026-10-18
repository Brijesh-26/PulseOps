"""Read-model assembly for API responses (queries kept out of route handlers)."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from pulseops.api.schemas import (
    DecisionOut,
    ExecutionOut,
    HypothesisOut,
    IncidentDetail,
    IncidentSummary,
    InvestigationOut,
    ProposalOut,
)
from pulseops.incidents.models import Incident, IncidentEvidence
from pulseops.investigator.models import (
    EvidenceRelation,
    HypothesisEvidence,
    InvestigationEvidence,
    InvestigationRun,
    RootCauseHypothesis,
)
from pulseops.remediation.models import (
    ApprovalDecision,
    ProposalStatus,
    RecoveryExecution,
    RecoveryProposal,
)


def summaries(session: Session, incidents: Sequence[Incident]) -> list[IncidentSummary]:
    ids = [i.id for i in incidents]
    latest_runs: dict[uuid.UUID, InvestigationRun] = {}
    pending: dict[uuid.UUID, uuid.UUID] = {}
    if ids:
        for run in session.scalars(
            select(InvestigationRun)
            .where(InvestigationRun.incident_id.in_(ids))
            .order_by(InvestigationRun.created_at)
        ):
            latest_runs[run.incident_id] = run
        for pid, iid in session.execute(
            select(RecoveryProposal.id, RecoveryProposal.incident_id).where(
                RecoveryProposal.incident_id.in_(ids),
                RecoveryProposal.status == ProposalStatus.PENDING_APPROVAL,
            )
        ):
            pending[iid] = pid
    out = []
    for i in incidents:
        latest = latest_runs.get(i.id)
        out.append(
            IncidentSummary(
                id=i.id,
                display_id=i.display_id,
                title=i.title,
                status=i.status.value,
                severity=i.severity.value,
                category=i.category.value,
                pipeline=i.pipeline.key,
                task=i.task.qualified_key if i.task else None,
                opened_at=i.opened_at,
                updated_at=i.updated_at,
                last_event_at=i.last_event_at,
                resolved_at=i.resolved_at,
                closed_at=i.closed_at,
                event_count=i.event_count,
                version=i.version,
                investigation_status=latest.status.value if latest else None,
                latest_confidence=latest.overall_confidence if latest else None,
                pending_proposal_id=pending.get(i.id),
            )
        )
    return out


def investigations(session: Session, runs: Sequence[InvestigationRun]) -> list[InvestigationOut]:
    run_ids = [r.id for r in runs]
    codes_by_run: dict[uuid.UUID, list[str]] = defaultdict(list)
    hyps_by_run: dict[uuid.UUID, list[RootCauseHypothesis]] = defaultdict(list)
    links: dict[uuid.UUID, dict[EvidenceRelation, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    if run_ids:
        for run_id, code in session.execute(
            select(InvestigationEvidence.run_id, IncidentEvidence.code)
            .join(IncidentEvidence, IncidentEvidence.id == InvestigationEvidence.evidence_id)
            .where(InvestigationEvidence.run_id.in_(run_ids))
        ):
            codes_by_run[run_id].append(code)
        for h in session.scalars(
            select(RootCauseHypothesis)
            .where(RootCauseHypothesis.run_id.in_(run_ids))
            .order_by(RootCauseHypothesis.rank)
        ):
            hyps_by_run[h.run_id].append(h)
        hyp_ids = [h.id for hs in hyps_by_run.values() for h in hs]
        if hyp_ids:
            for hid, relation, code in session.execute(
                select(
                    HypothesisEvidence.hypothesis_id,
                    HypothesisEvidence.relation,
                    IncidentEvidence.code,
                )
                .join(IncidentEvidence, IncidentEvidence.id == HypothesisEvidence.evidence_id)
                .where(HypothesisEvidence.hypothesis_id.in_(hyp_ids))
            ):
                links[hid][relation].append(code)

    def sort_codes(codes: list[str]) -> list[str]:
        return sorted(codes, key=lambda c: int(c[1:]))

    return [
        InvestigationOut(
            id=r.id,
            status=r.status.value,
            trigger=r.trigger,
            requested_by=r.requested_by,
            provider=r.provider,
            model=r.model,
            schema_version=r.schema_version,
            attempts=r.attempts,
            created_at=r.created_at,
            started_at=r.started_at,
            finished_at=r.finished_at,
            error_code=r.error_code,
            error_message=r.error_message,
            summary=r.summary,
            overall_confidence=r.overall_confidence,
            result=r.result,
            validation_issues=r.validation_issues,
            tool_calls=r.tool_calls,
            evidence_codes=sort_codes(codes_by_run[r.id]),
            hypotheses=[
                HypothesisOut(
                    rank=h.rank,
                    title=h.title,
                    description=h.description,
                    category=h.category,
                    confidence=h.confidence,
                    is_supported=h.is_supported,
                    validation_notes=h.validation_notes,
                    supporting_evidence=sort_codes(links[h.id][EvidenceRelation.SUPPORTS]),
                    contradicting_evidence=sort_codes(links[h.id][EvidenceRelation.CONTRADICTS]),
                )
                for h in hyps_by_run[r.id]
            ],
        )
        for r in runs
    ]


def proposals(session: Session, rows: Sequence[RecoveryProposal]) -> list[ProposalOut]:
    ids = [p.id for p in rows]
    decisions = (
        {
            d.proposal_id: d
            for d in session.scalars(
                select(ApprovalDecision).where(ApprovalDecision.proposal_id.in_(ids))
            )
        }
        if ids
        else {}
    )
    executions = (
        {
            e.proposal_id: e
            for e in session.scalars(
                select(RecoveryExecution).where(RecoveryExecution.proposal_id.in_(ids))
            )
        }
        if ids
        else {}
    )
    out = []
    for p in rows:
        d, e = decisions.get(p.id), executions.get(p.id)
        out.append(
            ProposalOut(
                id=p.id,
                incident_id=p.incident_id,
                investigation_run_id=p.investigation_run_id,
                action_type=p.action_type.value,
                parameters=p.parameters,
                rationale=p.rationale,
                evidence_codes=p.evidence_codes,
                proposed_by=p.proposed_by,
                proposer_type=p.proposer_type.value,
                status=p.status.value,
                status_reason=p.status_reason,
                incident_version=p.incident_version,
                created_at=p.created_at,
                expires_at=p.expires_at,
                decision=DecisionOut(
                    decision=d.decision.value,
                    decided_by=d.decided_by,
                    decided_at=d.decided_at,
                    reason=d.reason,
                )
                if d
                else None,
                execution=ExecutionOut(
                    id=e.id,
                    status=e.status.value,
                    started_at=e.started_at,
                    finished_at=e.finished_at,
                    preconditions=e.preconditions,
                    output=e.output,
                    postcondition_ok=e.postcondition_ok,
                    error=e.error,
                )
                if e
                else None,
            )
        )
    return out


def detail(session: Session, incident: Incident) -> IncidentDetail:
    summary = summaries(session, [incident])[0]
    run = session.scalar(
        select(InvestigationRun)
        .where(InvestigationRun.incident_id == incident.id)
        .order_by(InvestigationRun.created_at.desc())
        .limit(1)
    )
    props = session.scalars(
        select(RecoveryProposal)
        .where(RecoveryProposal.incident_id == incident.id)
        .order_by(RecoveryProposal.created_at.desc())
    ).all()
    return IncidentDetail(
        **summary.model_dump(),
        fingerprint=incident.fingerprint,
        latest_investigation=investigations(session, [run])[0] if run else None,
        proposals=proposals(session, props),
    )
