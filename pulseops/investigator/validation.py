"""Backend validation of investigator output. The model proposes; the backend decides.

After strict schema validation, this module:
- removes citations of evidence codes that do not exist for the incident
  (fabricated IDs) and counts them as unsupported claims;
- drops observed facts left without valid evidence;
- marks hypotheses with no valid supporting evidence as unsupported and caps
  their confidence;
- forbids citing evidence the model itself flagged as suspicious as support;
- replaces model-claimed downstream impact with the backend-computed impact,
  flagging claims the dependency graph does not support;
- accepts a proposed action only if it is allowlisted, its parameters are valid
  and in scope, it cites valid evidence, and it is backed by a supported
  hypothesis with confidence at or above the configured threshold;
- flags text that claims an action was executed (the model cannot execute).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from pulseops.errors import ValidationFailedError
from pulseops.investigator.provider import ImpactBrief, MalformedOutputError
from pulseops.investigator.schema import InvestigationResultV1
from pulseops.remediation.actions import ActionScope, validate_action

UNSUPPORTED_CONFIDENCE_CAP = 0.2

_EXECUTION_CLAIM = re.compile(
    r"\b(I|we) (have )?(re)?(ran|run|executed|restarted|retried|re-ran|applied|fixed|marked)\b"
    r"|\b(has|have) been (executed|retried|restarted|applied)\b",
    re.IGNORECASE,
)


@dataclass
class ValidationIssue:
    type: str
    message: str
    path: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"type": self.type, "message": self.message, "path": self.path}


@dataclass
class ValidatedInvestigation:
    result: InvestigationResultV1
    issues: list[ValidationIssue] = field(default_factory=list)
    action: dict[str, Any] | None = None  # {"action_type", "parameters", ...} when accepted
    unsupported_claims: int = 0

    @property
    def issue_dicts(self) -> list[dict[str, str]]:
        return [i.as_dict() for i in self.issues]


def parse_output(raw: dict[str, Any], incident_id: str) -> InvestigationResultV1:
    try:
        result = InvestigationResultV1.model_validate(raw)
    except ValidationError as exc:
        errors = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:10]
        )
        raise MalformedOutputError(f"Output failed schema validation: {errors}") from exc
    if result.incident_id != incident_id:
        raise MalformedOutputError("Output references a different incident_id")
    return result


def validate_result(
    result: InvestigationResultV1,
    *,
    known_codes: set[str],
    impact: ImpactBrief,
    scope: ActionScope,
    min_action_confidence: float,
    backend_suspicious: set[str] | None = None,
) -> ValidatedInvestigation:
    issues: list[ValidationIssue] = []
    unsupported = 0

    def keep_known(codes: list[str], path: str) -> list[str]:
        nonlocal unsupported
        valid = []
        for code in codes:
            if code in known_codes:
                if code not in valid:
                    valid.append(code)
            else:
                unsupported += 1
                issues.append(
                    ValidationIssue("unknown_evidence_id", f"{code} does not exist", path)
                )
        return valid

    suspicious = set(keep_known(result.suspicious_evidence_ids, "suspicious_evidence_ids"))
    # Defence in depth: content the backend itself detects as instruction-like is
    # treated as suspicious even if the model did not flag it.
    for code in sorted((backend_suspicious or set()) - suspicious):
        if code in known_codes:
            suspicious.add(code)
            issues.append(
                ValidationIssue(
                    "suspicious_not_flagged",
                    f"{code} contains instruction-like content the model did not flag",
                    "suspicious_evidence_ids",
                )
            )

    facts = []
    for i, fact in enumerate(result.observed_facts):
        codes = keep_known(fact.evidence_ids, f"observed_facts[{i}]")
        if codes:
            facts.append(fact.model_copy(update={"evidence_ids": codes}))
        else:
            issues.append(
                ValidationIssue(
                    "unsupported_fact", "Fact dropped: no valid evidence", f"observed_facts[{i}]"
                )
            )

    hypotheses = []
    for i, hyp in enumerate(result.hypotheses):
        path = f"hypotheses[{i}]"
        support = keep_known(hyp.supporting_evidence_ids, path + ".supporting")
        contra = keep_known(hyp.contradicting_evidence_ids, path + ".contradicting")
        tainted = [c for c in support if c in suspicious]
        if tainted:
            issues.append(
                ValidationIssue(
                    "suspicious_support",
                    f"Suspicious evidence {tainted} cannot support a hypothesis",
                    path,
                )
            )
            support = [c for c in support if c not in suspicious]
        confidence = hyp.confidence
        if not support and hyp.category.value != "UNKNOWN":
            unsupported += 1
            confidence = min(confidence, UNSUPPORTED_CONFIDENCE_CAP)
            issues.append(
                ValidationIssue(
                    "unsupported_hypothesis",
                    f"No valid supporting evidence; confidence capped at {confidence}",
                    path,
                )
            )
        hypotheses.append(
            hyp.model_copy(
                update={
                    "supporting_evidence_ids": support,
                    "contradicting_evidence_ids": contra,
                    "confidence": confidence,
                }
            )
        )

    claimed_tasks = set(result.downstream_impact.affected_tasks)
    claimed_datasets = set(result.downstream_impact.affected_datasets)
    real_tasks = {t["task"] for t in impact.tasks}
    for extra in sorted(claimed_tasks - real_tasks):
        unsupported += 1
        issues.append(
            ValidationIssue(
                "unsupported_impact_claim",
                f"{extra} is not downstream in the dependency graph",
                "downstream_impact.affected_tasks",
            )
        )
    for extra in sorted(claimed_datasets - set(impact.datasets)):
        unsupported += 1
        issues.append(
            ValidationIssue(
                "unsupported_impact_claim",
                f"{extra} is not an affected dataset",
                "downstream_impact.affected_datasets",
            )
        )
    authoritative_impact = result.downstream_impact.model_copy(
        update={"affected_tasks": sorted(real_tasks), "affected_datasets": list(impact.datasets)}
    )

    for field_name in ("investigation_summary", "explanation"):
        if _EXECUTION_CLAIM.search(getattr(result, field_name)):
            issues.append(
                ValidationIssue(
                    "execution_claim",
                    "Text claims an action was performed; the investigator cannot execute actions",
                    field_name,
                )
            )

    accepted_action: dict[str, Any] | None = None
    proposed = result.proposed_action
    if proposed is not None:
        reason = None
        linked = next((h for h in hypotheses if h.rank == proposed.hypothesis_rank), None)
        action_codes = [
            c
            for c in keep_known(proposed.supporting_evidence_ids, "proposed_action")
            if c not in suspicious
        ]
        params: dict[str, Any] = {}
        try:
            params = validate_action(proposed.action_type, proposed.parameters.model_dump(), scope)
        except ValidationFailedError as exc:
            reason = f"{exc.code}: {exc.message}"
        if reason is None and not action_codes:
            reason = "action cites no valid, non-suspicious evidence"
        if reason is None and (linked is None or not linked.supporting_evidence_ids):
            reason = "action is not linked to a supported hypothesis"
        if reason is None and linked is not None and linked.confidence < min_action_confidence:
            reason = (
                f"hypothesis confidence {linked.confidence:.2f} is below the "
                f"{min_action_confidence:.2f} threshold for proposing actions"
            )
        if reason is None:
            accepted_action = {
                "action_type": proposed.action_type.value,
                "parameters": params,
                "rationale": proposed.rationale,
                "evidence_codes": action_codes,
                "hypothesis_rank": proposed.hypothesis_rank,
            }
        else:
            issues.append(ValidationIssue("unjustified_action", reason, "proposed_action"))

    sanitized = result.model_copy(
        update={
            "observed_facts": facts,
            "hypotheses": hypotheses,
            "supporting_evidence_ids": keep_known(
                result.supporting_evidence_ids, "supporting_evidence_ids"
            ),
            "contradicting_evidence_ids": keep_known(
                result.contradicting_evidence_ids, "contradicting_evidence_ids"
            ),
            "suspicious_evidence_ids": sorted(suspicious),
            "downstream_impact": authoritative_impact,
            "proposed_action": proposed if accepted_action else None,
        }
    )
    return ValidatedInvestigation(sanitized, issues, accepted_action, unsupported)
