# Evaluation

## How to reproduce

```bash
docker compose up -d postgres
uv run python -m simulator evaluate --provider mock --out reports
# with a real model (paid API calls):
PULSEOPS_ANTHROPIC_API_KEY=... uv run python -m simulator evaluate --provider anthropic --out reports
```

The command:

1. Drops and recreates the `pulseops_eval` database and runs migrations.
2. Runs each scenario end to end, in process and deterministically: seed → ingest → relay → worker → investigation.
3. Scores what was **actually stored**.

`tests/evaluation/test_scenarios.py` runs the same harness in CI.

## What is measured

| Metric | Definition |
|---|---|
| Root cause correct | Category of the top hypothesis equals the expected category (scenarios that define one) |
| Action correct | The proposal that was created equals the expected action, where "none" must stay none |
| Unjustified action | An action was proposed in a scenario that expects none |
| Confidence within bounds | Overall confidence inside the scenario's `[min, max]` |
| Invalid citations | Stored result cites evidence codes that do not exist (must be 0) |
| Evidence match | The cited evidence contains the expected signal (for example `timeout expired`, `upstream_failed`, a `BASELINE` item) |
| Impact | Graph traversal equals the expected tasks, datasets and paths, and the stored result's impact equals the graph |
| Missing information / suspicious | Required where the scenario demands uncertainty or injection handling |
| Flagged claims | Validator issues of type `unknown_evidence_id` / `unsupported_*` |

## Results: mock provider

Generated from an actual run. The block below is the unedited report file.

## Summary

```json
{
  "scenarios": 5,
  "passed": 5,
  "root_cause_accuracy": 1.0,
  "root_cause_scored_scenarios": 4,
  "action_accuracy": 1.0,
  "unjustified_actions": 0,
  "invalid_citations_in_stored_results": 0,
  "unsupported_claims_flagged_by_validator": 0
}
```

## Per scenario

| Scenario | Root cause (expected → actual) | Action (expected → actual) | Confidence | Cited | Invalid cites | Flagged claims | Impact ok | Pass |
|---|---|---|---|---|---|---|---|---|
| a_db_connection_failure | DATABASE_CONNECTIVITY → DATABASE_CONNECTIVITY | CHECK_DATABASE_CONNECTIVITY → CHECK_DATABASE_CONNECTIVITY | 0.85 | 25 | 0 | 0 | yes | yes |
| b_slow_pipeline | UNKNOWN → UNKNOWN | none → none | 0.15 | 3 | 0 | 0 | yes | yes |
| c_upstream_dependency_failure | UPSTREAM_DEPENDENCY_FAILURE → UPSTREAM_DEPENDENCY_FAILURE | MARK_DATASET_STALE → MARK_DATASET_STALE | 0.8 | 28 | 0 | 0 | yes | yes |
| d_duplicate_delivery | TEST_FIXTURE_CORRUPTION → TEST_FIXTURE_CORRUPTION | REPROCESS_TEST_FIXTURE → REPROCESS_TEST_FIXTURE | 0.75 | 7 | 0 | 0 | yes | yes |
| e_misleading_evidence | — → DATABASE_CONNECTIVITY | none → none | 0.3 | 9 | 0 | 0 | yes | yes |

## Notes

- none
## Interpretation

- **The scope of the mock result.** The mock provider is a deterministic rule engine written by the same author as the scenarios. A perfect score demonstrates that ingestion, deduplication, correlation, evidence collection, validation, impact computation and uncertainty handling work end to end. It does **not** demonstrate language-model reasoning quality. In Scenario E the top hypothesis is a low-confidence (0.30) database hypothesis. That scenario has no expected category; what it checks is that no action is proposed, suspicious content is flagged, and missing information is listed.
- **Guardrail effectiveness** is tested with adversarial scripted providers in `tests/integration/test_investigation.py`. In those tests every case is caught:
  - fabricated evidence IDs are stripped and counted;
  - facts left without support are dropped;
  - unsupported hypotheses have their confidence capped;
  - actions are dropped when out of scope, below the confidence threshold or without evidence;
  - fake impact claims are replaced by graph impact;
  - execution claims are flagged;
  - malformed or unlisted output fails the run with no side effects;
  - a provider that cites prompt-injected evidence without flagging it gets no proposal.
- **Claude provider: not yet evaluated.** No run against the real API has been performed, so no accuracy number exists. The loop, error mapping, refusal handling, tool scoping and submission validation are tested with a fake client.
- **Not measured:** calibration of confidence, latency and cost per investigation, and robustness on real production logs.
