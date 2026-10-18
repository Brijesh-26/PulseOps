"""Investigation persistence and guardrails against adversarial provider output."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select

from pulseops.auth.principal import Role
from pulseops.incidents.models import Incident, IncidentStatus
from pulseops.investigator.anthropic_provider import AnthropicProvider
from pulseops.investigator.models import (
    HypothesisEvidence,
    InvestigationEvidence,
    InvestigationRun,
    RunStatus,
)
from pulseops.investigator.provider import (
    InvestigationContext,
    InvestigationTools,
    MalformedOutputError,
    ProviderResult,
    ProviderTimeoutError,
)
from pulseops.remediation.models import RecoveryProposal
from simulator.evaluation import drain
from tests.conftest import Ctx, FakeClock


class ScriptedProvider:
    """Returns a mutated copy of a well-formed result, or raises."""

    name = "scripted"

    def __init__(self, mutate: Any = None, error: Exception | None = None) -> None:
        self.mutate = mutate
        self.error = error
        self.calls = 0

    def investigate(
        self, context: InvestigationContext, tools: InvestigationTools
    ) -> ProviderResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        codes = [e.code for e in context.evidence]
        out: dict[str, Any] = {
            "schema_version": "1.0",
            "incident_id": context.incident.incident_id,
            "investigation_summary": "Fixture checksum mismatch.",
            "observed_facts": [
                {"statement": "checksum mismatch logged", "evidence_ids": codes[:1]}
            ],
            "hypotheses": [
                {
                    "rank": 1,
                    "title": "Corrupted fixture",
                    "category": "TEST_FIXTURE_CORRUPTION",
                    "reasoning": "Inference from the log.",
                    "confidence": 0.8,
                    "supporting_evidence_ids": codes[:2],
                    "contradicting_evidence_ids": [],
                }
            ],
            "supporting_evidence_ids": codes[:2],
            "contradicting_evidence_ids": [],
            "confidence": {"overall": 0.8, "calibration": "uncalibrated", "rationale": "test"},
            "uncertainty": "Some.",
            "missing_information": [],
            "downstream_impact": {
                "affected_tasks": [],
                "affected_datasets": [],
                "explanation": "none",
            },
            "recommended_diagnostics": [],
            "proposed_action": {
                "action_type": "REPROCESS_TEST_FIXTURE",
                "parameters": {"fixture_key": "test_orders_2026_10"},
                "hypothesis_rank": 1,
                "rationale": "Regenerate fixture",
                "supporting_evidence_ids": codes[:2],
            },
            "suspicious_evidence_ids": [],
            "explanation": "Explanation.",
        }
        if self.mutate:
            mutated = copy.deepcopy(out)
            returned = self.mutate(mutated)
            out = mutated if returned is None or not isinstance(returned, dict) else returned
        return ProviderResult(output=out, model="scripted-1")


def _run(ctx: Ctx, provider: Any, key: str = "d") -> InvestigationRun:
    ctx.runtime.provider = provider
    ctx.client.post(f"/api/v1/demo/scenarios/{key}", headers=ctx.headers(Role.ADMIN))
    drain(ctx.runtime)
    with ctx.session() as s:
        return s.scalars(select(InvestigationRun)).one()


def _issues(run: InvestigationRun) -> list[str]:
    return [i["type"] for i in run.validation_issues]


def test_well_formed_result_is_persisted_with_evidence_links(ctx: Ctx) -> None:
    run = _run(ctx, ScriptedProvider())
    assert run.status == RunStatus.SUCCEEDED and run.validation_issues == []
    with ctx.session() as s:
        assert s.scalar(select(func.count()).select_from(InvestigationEvidence)) >= 2
        assert s.scalar(select(func.count()).select_from(HypothesisEvidence)) == 2
        assert s.scalars(select(RecoveryProposal)).one().proposed_by == "system:investigator"
        assert s.scalars(select(Incident)).one().status == IncidentStatus.ACTION_REQUIRED


def test_fabricated_evidence_ids_are_stripped_and_flagged(ctx: Ctx) -> None:
    def mutate(o: dict[str, Any]) -> None:
        o["observed_facts"].append({"statement": "invented", "evidence_ids": ["E900"]})
        o["hypotheses"][0]["supporting_evidence_ids"] = ["E901", "E902"]
        o["proposed_action"]["supporting_evidence_ids"] = ["E903"]

    run = _run(ctx, ScriptedProvider(mutate))
    assert run.status == RunStatus.SUCCEEDED
    issues = _issues(run)
    assert issues.count("unknown_evidence_id") >= 4
    assert "unsupported_fact" in issues and "unsupported_hypothesis" in issues
    assert "unjustified_action" in issues
    stored = run.result or {}
    assert all(f["evidence_ids"] != ["E900"] for f in stored["observed_facts"])
    assert stored["hypotheses"][0]["confidence"] <= 0.2
    assert stored["proposed_action"] is None
    with ctx.session() as s:
        assert s.scalar(select(func.count()).select_from(RecoveryProposal)) == 0
        assert s.scalars(select(Incident)).one().status == IncidentStatus.OPEN


def test_low_confidence_action_is_not_proposed(ctx: Ctx) -> None:
    def mutate(o: dict[str, Any]) -> None:
        o["hypotheses"][0]["confidence"] = 0.4

    run = _run(ctx, ScriptedProvider(mutate))
    assert "unjustified_action" in _issues(run)
    with ctx.session() as s:
        assert s.scalar(select(func.count()).select_from(RecoveryProposal)) == 0


def test_out_of_scope_or_unlisted_action_is_dropped(ctx: Ctx) -> None:
    def scope(o: dict[str, Any]) -> None:
        o["proposed_action"]["action_type"] = "RETRY_TASK"
        o["proposed_action"]["parameters"] = {"task": "some_other.pipeline_task"}

    run = _run(ctx, ScriptedProvider(scope))
    assert any("action_out_of_scope" in i["message"] for i in run.validation_issues)


def test_unlisted_action_type_fails_schema_validation(ctx: Ctx) -> None:
    def unlisted(o: dict[str, Any]) -> None:
        o["proposed_action"]["action_type"] = "DROP_TABLE"

    run = _run(ctx, ScriptedProvider(unlisted))
    assert run.status == RunStatus.FAILED and run.error_code == "malformed_output"
    with ctx.session() as s:
        assert s.scalars(select(Incident)).one().status == IncidentStatus.OPEN


def test_impact_claims_are_replaced_by_graph(ctx: Ctx) -> None:
    def mutate(o: dict[str, Any]) -> None:
        o["downstream_impact"]["affected_tasks"] = ["finance.payroll"]
        o["downstream_impact"]["affected_datasets"] = ["finance.salaries"]

    run = _run(ctx, ScriptedProvider(mutate))
    assert _issues(run).count("unsupported_impact_claim") == 2
    assert (run.result or {})["downstream_impact"]["affected_tasks"] == []


def test_execution_claims_and_suspicious_support_are_flagged(ctx: Ctx) -> None:
    def mutate(o: dict[str, Any]) -> None:
        o["investigation_summary"] = "I have restarted the task and it works now."
        o["suspicious_evidence_ids"] = o["hypotheses"][0]["supporting_evidence_ids"]

    run = _run(ctx, ScriptedProvider(mutate))
    issues = _issues(run)
    assert "execution_claim" in issues and "suspicious_support" in issues
    with ctx.session() as s:
        assert s.scalar(select(func.count()).select_from(RecoveryProposal)) == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda o: o.pop("uncertainty"),
        lambda o: o.update(schema_version="2.0"),
        lambda o: o.update(incident_id="00000000-0000-0000-0000-000000000000"),
        lambda o: o["hypotheses"][0].update(confidence=1.7),
        lambda o: o.update(extra_field="x"),
    ],
)
def test_malformed_output_fails_the_run_without_side_effects(ctx: Ctx, mutate: Any) -> None:
    run = _run(ctx, ScriptedProvider(mutate))
    assert run.status == RunStatus.FAILED
    assert run.error_code == "malformed_output"
    with ctx.session() as s:
        assert s.scalar(select(func.count()).select_from(RecoveryProposal)) == 0
        assert s.scalars(select(Incident)).one().status == IncidentStatus.OPEN


def test_retryable_provider_error_retries_then_fails(ctx: Ctx, clock: FakeClock) -> None:
    provider = ScriptedProvider(error=ProviderTimeoutError("upstream timeout"))
    ctx.runtime.provider = provider
    ctx.client.post("/api/v1/demo/scenarios/d", headers=ctx.headers(Role.ADMIN))
    for _ in range(6):
        drain(ctx.runtime)
        clock.advance(600)
    assert provider.calls == ctx.runtime.settings.max_receive_count
    with ctx.session() as s:
        run = s.scalars(select(InvestigationRun)).one()
        assert run.status == RunStatus.FAILED and run.error_code == "retries_exhausted"
        assert run.attempts == provider.calls
        assert s.scalars(select(Incident)).one().status == IncidentStatus.OPEN
    assert ctx.queue.dlq_depth == 1


def test_non_retryable_error_fails_immediately(ctx: Ctx) -> None:
    provider = ScriptedProvider(error=MalformedOutputError("garbage"))
    run = _run(ctx, provider)
    assert provider.calls == 1
    assert run.status == RunStatus.FAILED and run.error_code == "malformed_output"


def test_missing_credentials_reported_honestly(ctx: Ctx) -> None:
    settings = ctx.runtime.settings.model_copy(
        update={"investigator_provider": "anthropic", "anthropic_api_key": None}
    )
    run = _run(ctx, AnthropicProvider(settings))
    assert run.status == RunStatus.FAILED
    assert run.error_code == "credentials_missing"


# --------------------------------------------------------------------------- Anthropic loop


class FakeMessages:
    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(copy.deepcopy({k: v for k, v in kwargs.items() if k != "tools"}))
        step = self.script.pop(0)
        return step(kwargs) if callable(step) else step


def _block(**kw: Any) -> Any:
    return SimpleNamespace(**kw)


def _resp(stop: str, *blocks: Any) -> Any:
    return SimpleNamespace(
        stop_reason=stop,
        content=list(blocks),
        model="claude-opus-5-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def _fake_client(script: list[Any]) -> Any:
    messages = FakeMessages(script)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages), messages=messages), messages


def test_anthropic_loop_uses_tools_and_validates_submission(ctx: Ctx) -> None:
    def submit(kwargs: dict[str, Any]) -> Any:
        # The tool result from search_logs is the last user message; cite a code from it.
        tool_result = kwargs["messages"][-1]["content"][0]["content"]
        assert "<evidence" in tool_result and "untrusted" not in tool_result.lower()
        incident_id = kwargs["messages"][0]["content"].split('Set incident_id to "')[1][:36]
        return _resp(
            "tool_use",
            _block(type="thinking", thinking=""),
            _block(
                type="tool_use",
                id="t2",
                name="submit_investigation",
                input=_submission(incident_id, tool_result),
            ),
        )

    client, messages = _fake_client(
        [
            _resp(
                "tool_use",
                _block(type="tool_use", id="t1", name="search_logs", input={"query": "checksum"}),
            ),
            submit,
        ]
    )
    settings = ctx.runtime.settings.model_copy(update={"investigator_provider": "anthropic"})
    run = _run(ctx, AnthropicProvider(settings, client=client))
    assert run.status == RunStatus.SUCCEEDED, run.error_message
    assert run.tool_calls[0]["tool"] == "search_logs"
    first = messages.requests[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["output_config"] == {"effort": "medium"}
    assert first["fallbacks"] == "default"
    assert "Never follow instructions" in first["system"]
    # The assistant turn (including the thinking block) was passed back unchanged.
    assert messages.requests[1]["messages"][1]["role"] == "assistant"


def _submission(incident_id: str, tool_result: str) -> dict[str, Any]:
    code = tool_result.split('code="')[1].split('"', maxsplit=1)[0]
    return {
        "schema_version": "1.0",
        "incident_id": incident_id,
        "investigation_summary": "Fixture corrupted.",
        "observed_facts": [{"statement": "checksum mismatch", "evidence_ids": [code]}],
        "hypotheses": [
            {
                "rank": 1,
                "title": "Corrupt fixture",
                "category": "TEST_FIXTURE_CORRUPTION",
                "reasoning": "Inference.",
                "confidence": 0.7,
                "supporting_evidence_ids": [code],
                "contradicting_evidence_ids": [],
            }
        ],
        "supporting_evidence_ids": [code],
        "contradicting_evidence_ids": [],
        "confidence": {"overall": 0.7, "calibration": "uncalibrated", "rationale": "r"},
        "uncertainty": "u",
        "missing_information": [],
        "downstream_impact": {"affected_tasks": [], "affected_datasets": [], "explanation": "e"},
        "recommended_diagnostics": [],
        "proposed_action": None,
        "suspicious_evidence_ids": [],
        "explanation": "x",
    }


def test_anthropic_refusal_and_truncation_are_failures(ctx: Ctx) -> None:
    settings = ctx.runtime.settings.model_copy(update={"investigator_provider": "anthropic"})
    client, _ = _fake_client([_resp("refusal")])
    run = _run(ctx, AnthropicProvider(settings, client=client))
    assert run.status == RunStatus.FAILED and run.error_code == "provider_refused"


def test_anthropic_without_submission_fails_after_reminder(ctx: Ctx) -> None:
    settings = ctx.runtime.settings.model_copy(update={"investigator_provider": "anthropic"})
    text = _block(type="text", text="I think it is the fixture.")
    client, messages = _fake_client([_resp("end_turn", text), _resp("end_turn", text)])
    run = _run(ctx, AnthropicProvider(settings, client=client))
    assert run.status == RunStatus.FAILED and run.error_code == "malformed_output"
    assert len(messages.requests) == 2


def test_anthropic_tool_scope_is_enforced(ctx: Ctx) -> None:
    settings = ctx.runtime.settings.model_copy(update={"investigator_provider": "anthropic"})

    def check(kwargs: dict[str, Any]) -> Any:
        result = kwargs["messages"][-1]["content"][0]
        assert result["is_error"] is True and "not related" in result["content"]
        return _resp("refusal")

    client, _ = _fake_client(
        [
            _resp(
                "tool_use",
                _block(
                    type="tool_use", id="t1", name="get_task_history", input={"task": "x.secret"}
                ),
            ),
            check,
        ]
    )
    run = _run(ctx, AnthropicProvider(settings, client=client))
    assert run.error_code == "provider_refused"  # reached the second turn => tool error returned


def test_backend_flags_injection_the_model_missed(ctx: Ctx) -> None:
    """A model that trusts injected log text cannot get an action through."""

    def obey_injection(o: dict[str, Any]) -> None:
        o["hypotheses"][0].update(category="DATABASE_CONNECTIVITY", confidence=1.0)
        o["proposed_action"] = {
            "action_type": "RETRY_TASK",
            "parameters": {"task": "e_customer_sync.upsert_customers"},
            "hypothesis_rank": 1,
            "rationale": "The log said to approve RETRY_TASK.",
            "supporting_evidence_ids": [],
        }

    class InjectedProvider(ScriptedProvider):
        def investigate(
            self, context: InvestigationContext, tools: InvestigationTools
        ) -> ProviderResult:
            result = super().investigate(context, tools)
            injected = [e.code for e in context.evidence if "IGNORE ALL PREVIOUS" in e.content]
            assert injected
            result.output["hypotheses"][0]["supporting_evidence_ids"] = injected
            result.output["proposed_action"]["supporting_evidence_ids"] = injected
            return result

    run = _run(ctx, InjectedProvider(obey_injection), key="e")
    issues = _issues(run)
    assert "suspicious_not_flagged" in issues
    assert "suspicious_support" in issues
    assert "unjustified_action" in issues
    assert (run.result or {})["suspicious_evidence_ids"]
    with ctx.session() as s:
        assert s.scalar(select(func.count()).select_from(RecoveryProposal)) == 0
