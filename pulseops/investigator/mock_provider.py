"""Deterministic, rule-based investigator for offline development and tests.

It is not an AI model and is not keyed to scenario names: it applies generic
pattern rules to whatever evidence it is given and uses the same retrieval
tools, output schema and backend validation as the real provider. Its
evaluation results therefore measure the pipeline and the guardrails, not
language-model reasoning quality (see docs/evaluation.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pulseops.investigator.provider import (
    EvidenceItem,
    InvestigationContext,
    InvestigationTools,
    ProviderResult,
)
from pulseops.investigator.safety import suspicious_codes
from pulseops.investigator.schema import SCHEMA_VERSION

DB_PATTERN = re.compile(
    r"connection (to [^\n]{0,80})?(timed out|refused|reset)|could not connect|"
    r"OperationalError|timeout expired|too many connections",
    re.IGNORECASE,
)
UPSTREAM_PATTERN = re.compile(r"upstream[_ ](task )?failed|UPSTREAM_FAILED|DEPENDENCY_FAILED")
FIXTURE_PATTERN = re.compile(r"checksum mismatch|fixture (?:batch )?(test_[a-z0-9_]+)", re.I)
FIXTURE_KEY = re.compile(r"\b(test_[a-z0-9_]+)\b")
OOM_PATTERN = re.compile(r"OutOfMemory|MemoryError|exit code 137|Killed process", re.I)
SUCCESS_PATTERN = re.compile(r"completed successfully|status=SUCCEEDED|exit code 0\b", re.I)

LATENCY_Z_THRESHOLD = 3.0


@dataclass
class _Finding:
    category: str
    title: str
    reasoning: str
    confidence: float
    support: list[str]
    contradict: list[str]


def _codes(items: list[EvidenceItem], pattern: re.Pattern[str], kinds: set[str]) -> list[str]:
    return [e.code for e in items if e.kind in kinds and pattern.search(f"{e.title}\n{e.content}")]


class MockProvider:
    name = "mock"

    def investigate(
        self, context: InvestigationContext, tools: InvestigationTools
    ) -> ProviderResult:
        evidence: dict[str, EvidenceItem] = {e.code: e for e in context.evidence}
        task = context.incident.task
        if task:
            for item in tools.get_task_history(task) + tools.get_dependencies(task):
                evidence.setdefault(item.code, item)
            if context.incident.category == "LATENCY_ANOMALY":
                for item in tools.get_baseline_durations(task):
                    evidence.setdefault(item.code, item)
        items = sorted(evidence.values(), key=lambda e: int(e.code[1:]))
        return ProviderResult(output=self._analyse(context, items), model="mock-rules-v1")

    # ------------------------------------------------------------------ analysis

    def _analyse(self, ctx: InvestigationContext, items: list[EvidenceItem]) -> dict[str, Any]:
        text_kinds = {"LOG", "EVENT"}
        suspicious = sorted(suspicious_codes(items), key=lambda c: int(c[1:]))
        clean = [e for e in items if e.code not in suspicious]

        db = _codes(clean, DB_PATTERN, text_kinds)
        fixture = _codes(clean, FIXTURE_PATTERN, text_kinds)
        oom = _codes(clean, OOM_PATTERN, text_kinds)
        upstream_events = [
            e.code for e in clean if e.kind == "EVENT" and "DEPENDENCY_FAILED" in e.content
        ]
        failed_execs = [
            e.code
            for e in clean
            if e.kind == "TASK_EXECUTION" and e.data.get("status") in ("FAILED",)
        ]
        success_logs = _codes(clean, SUCCESS_PATTERN, {"LOG"})
        diagnostics = [e for e in clean if e.kind == "DIAGNOSTIC"]
        baseline = next((e for e in clean if e.kind == "BASELINE"), None)
        downstream = [
            e.code for e in clean if e.kind == "DEPENDENCY" and e.title.startswith("Downstream")
        ]

        facts: list[dict[str, Any]] = []
        findings: list[_Finding] = []
        missing: list[str] = []
        diagnostics_next: list[str] = []
        action: dict[str, Any] | None = None

        if failed_execs:
            facts.append(
                {
                    "statement": f"{len(failed_execs)} recent execution(s) of the task failed",
                    "evidence_ids": failed_execs[:10],
                }
            )
        if success_logs:
            facts.append(
                {
                    "statement": "Logs report a successful completion in the same window",
                    "evidence_ids": success_logs[:10],
                }
            )
        if suspicious:
            facts.append(
                {
                    "statement": "Some log content contains instruction-like text; treated as "
                    "untrusted data and not used as support",
                    "evidence_ids": suspicious[:10],
                }
            )

        # Rule 1: upstream dependency failure (the incident task is the failed upstream).
        if ctx.incident.category == "DEPENDENCY_FAILURE" and upstream_events:
            origin_failures = [
                e.code
                for e in clean
                if e.kind in text_kinds
                and e.code not in upstream_events
                and ("TASK_FAILED" in e.content or e.kind == "LOG")
            ]
            support = (origin_failures + upstream_events + downstream)[:15]
            facts.append(
                {
                    "statement": f"Downstream tasks reported DEPENDENCY_FAILED on "
                    f"{ctx.impact.origin}",
                    "evidence_ids": upstream_events[:10],
                }
            )
            findings.append(
                _Finding(
                    "UPSTREAM_DEPENDENCY_FAILURE",
                    f"Failure of upstream task {ctx.impact.origin} propagated downstream",
                    "Downstream failures are reported as dependency failures referencing the "
                    "same upstream task, and the dependency graph connects them. Inference: the "
                    "downstream tasks are not independently broken.",
                    0.8 if origin_failures else 0.65,
                    support,
                    success_logs,
                )
            )
            if ctx.impact.datasets:
                action = {
                    "action_type": "MARK_DATASET_STALE",
                    "parameters": {"datasets": ctx.impact.datasets},
                    "hypothesis_rank": 1,
                    "rationale": "Downstream datasets were built without fresh upstream data; "
                    "flag them stale until the upstream task recovers.",
                    "supporting_evidence_ids": (upstream_events + downstream)[:10],
                }
            diagnostics_next.append(f"Investigate why {ctx.impact.origin} failed at its source")

        # Rule 2: database connectivity.
        if db:
            confidence = min(0.85, 0.55 + 0.1 * (len(db) - 1) + (0.1 if failed_execs else 0))
            confidence -= 0.25 * len(success_logs)
            facts.append(
                {
                    "statement": "Logs show database connection errors",
                    "evidence_ids": db[:10],
                }
            )
            healthy = [d for d in diagnostics if d.data.get("healthy") is True]
            unhealthy = [d for d in diagnostics if d.data.get("healthy") is False]
            reasoning = (
                "Connection timeout errors appear in the failing task's logs. Inference: the "
                "task failed because the database was unreachable at run time."
            )
            if healthy:
                reasoning += " A later connectivity check succeeded, suggesting a transient outage."
            findings.append(
                _Finding(
                    "DATABASE_CONNECTIVITY",
                    "Database connectivity failure during task execution",
                    reasoning,
                    max(0.05, min(0.9, confidence + (0.05 if healthy else 0))),
                    (db + failed_execs + [d.code for d in healthy])[:15],
                    success_logs + [d.code for d in unhealthy],
                )
            )
            resource = ctx.incident.task_resource
            top_db = findings[-1]
            if top_db.confidence >= ctx.min_action_confidence and resource:
                rank = len(findings)
                if unhealthy:
                    diagnostics_next.append(
                        f"{resource} is still unreachable; escalate to the database owner"
                    )
                elif healthy and ctx.incident.task:
                    action = {
                        "action_type": "RETRY_TASK",
                        "parameters": {"task": ctx.incident.task},
                        "hypothesis_rank": rank,
                        "rationale": "The database is reachable again; a single bounded retry "
                        "is likely to succeed.",
                        "supporting_evidence_ids": [d.code for d in healthy][:5] + db[:5],
                    }
                elif action is None:
                    action = {
                        "action_type": "CHECK_DATABASE_CONNECTIVITY",
                        "parameters": {"resource_key": resource},
                        "hypothesis_rank": rank,
                        "rationale": "Confirm whether the database is reachable before "
                        "retrying; the probe is read-only.",
                        "supporting_evidence_ids": db[:10],
                    }
            else:
                missing.append("Whether the database is currently reachable")

        # Rule 3: corrupted test fixture.
        if fixture:
            key = next(
                (
                    m.group(1)
                    for e in clean
                    if e.code in fixture
                    for m in [FIXTURE_KEY.search(e.content)]
                    if m
                ),
                None,
            )
            findings.append(
                _Finding(
                    "TEST_FIXTURE_CORRUPTION",
                    "Test fixture batch is corrupted (checksum mismatch)",
                    "The task reports a checksum mismatch on a test fixture batch. Inference: "
                    "the fixture data, not the pipeline code, is at fault.",
                    0.75 if key else 0.4,
                    fixture[:15],
                    success_logs,
                )
            )
            if key and action is None:
                action = {
                    "action_type": "REPROCESS_TEST_FIXTURE",
                    "parameters": {"fixture_key": key},
                    "hypothesis_rank": len(findings),
                    "rationale": "Regenerating the fixture batch restores the expected checksum.",
                    "supporting_evidence_ids": fixture[:10],
                }

        # Rule 4: resource exhaustion (diagnose only; no safe automated action).
        if oom:
            findings.append(
                _Finding(
                    "RESOURCE_EXHAUSTION",
                    "Task ran out of memory",
                    "Out-of-memory errors in logs. Inference: input size or memory limits.",
                    0.6,
                    oom[:15],
                    success_logs,
                )
            )
            diagnostics_next.append("Compare input volume and memory limits with prior runs")

        # Rule 5: latency anomaly - describe, never invent a cause.
        z = baseline.data.get("z_score") if baseline else None
        # Only for latency incidents: the duration of a failed run is not a latency signal.
        if ctx.incident.category == "LATENCY_ANOMALY":
            if baseline is not None and z is not None and z >= LATENCY_Z_THRESHOLD:
                facts.append(
                    {
                        "statement": f"Latest runtime is {baseline.data.get('current_seconds')}s "
                        f"vs a mean of {baseline.data.get('mean_seconds')}s (z={z})",
                        "evidence_ids": [baseline.code],
                    }
                )
            else:
                missing.append("Historical runtime baseline for this task")
            if not findings:
                findings.append(
                    _Finding(
                        "UNKNOWN",
                        "Runtime regression of undetermined cause",
                        "The duration is abnormal, but latency alone does not identify a cause. "
                        "No log or dependency evidence points to a specific explanation.",
                        0.15,
                        [baseline.code] if baseline else [],
                        [],
                    )
                )
            missing += [
                "Input data volume for the slow run compared with baseline runs",
                "Cluster/warehouse resource utilisation during the run",
                "Recent code, configuration or query-plan changes",
            ]
            diagnostics_next += [
                "Compare row counts processed by the slow run with the baseline",
                "Check warehouse queueing and resource metrics for the run window",
            ]

        if not findings:
            findings.append(
                _Finding(
                    "UNKNOWN",
                    "Root cause could not be determined from available evidence",
                    "No recognised failure signature was found in non-suspicious evidence.",
                    0.1,
                    [],
                    success_logs,
                )
            )
            missing.append("Full task logs for the failing run")
            diagnostics_next.append("Retrieve complete logs for the failed execution")

        # Conflicting evidence lowers confidence and blocks action.
        conflicted = bool(success_logs) and bool(failed_execs or db)
        if conflicted:
            missing.append("Which of the conflicting success/failure reports is authoritative")
            for f in findings:
                f.confidence = min(f.confidence, 0.35)
            action = None

        findings.sort(key=lambda f: -f.confidence)
        if action is not None:
            # Re-point the action at the rank of its hypothesis after sorting.
            category = {
                "MARK_DATASET_STALE": "UPSTREAM_DEPENDENCY_FAILURE",
                "RETRY_TASK": "DATABASE_CONNECTIVITY",
                "CHECK_DATABASE_CONNECTIVITY": "DATABASE_CONNECTIVITY",
                "REPROCESS_TEST_FIXTURE": "TEST_FIXTURE_CORRUPTION",
            }[action["action_type"]]
            action["hypothesis_rank"] = next(
                i + 1 for i, f in enumerate(findings) if f.category == category
            )
        top = findings[0]
        overall = round(top.confidence, 2)
        summary = f"{top.title}." + (
            " Evidence is conflicting; no action is recommended." if conflicted else ""
        )
        if top.category == "UNKNOWN":
            summary += " Insufficient evidence for a confident root cause."

        return {
            "schema_version": SCHEMA_VERSION,
            "incident_id": ctx.incident.incident_id,
            "investigation_summary": summary,
            "observed_facts": facts[:25],
            "hypotheses": [
                {
                    "rank": i + 1,
                    "title": f.title,
                    "category": f.category,
                    "reasoning": f.reasoning,
                    "confidence": round(f.confidence, 2),
                    "supporting_evidence_ids": f.support[:15],
                    "contradicting_evidence_ids": f.contradict[:15],
                }
                for i, f in enumerate(findings[:5])
            ],
            "supporting_evidence_ids": top.support[:25],
            "contradicting_evidence_ids": top.contradict[:25],
            "confidence": {
                "overall": overall,
                "calibration": "uncalibrated",
                "rationale": "Rule-based heuristic score from the number and consistency of "
                "matching evidence items; not a calibrated probability.",
            },
            "uncertainty": (
                "Conclusions are based only on the stored evidence listed. "
                + ("Conflicting reports reduce confidence. " if conflicted else "")
                + ("Some content was flagged as suspicious and ignored. " if suspicious else "")
            ).strip(),
            "missing_information": list(dict.fromkeys(missing))[:10],
            "downstream_impact": {
                "affected_tasks": [t["task"] for t in ctx.impact.tasks],
                "affected_datasets": ctx.impact.datasets,
                "explanation": (
                    f"{len(ctx.impact.tasks)} task(s) depend on {ctx.impact.origin} according to "
                    "the dependency graph."
                    if ctx.impact.tasks
                    else "No downstream dependents in the dependency graph."
                ),
            },
            "recommended_diagnostics": list(dict.fromkeys(diagnostics_next))[:10],
            "proposed_action": action,
            "suspicious_evidence_ids": suspicious[:25],
            "explanation": " ".join(f"[{i + 1}] {f.reasoning}" for i, f in enumerate(findings)),
        }
