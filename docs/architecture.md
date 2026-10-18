# Architecture

## Processes

| Process | Entry point | Responsibility |
|---|---|---|
| `api` | `uvicorn apps.api.main:app` | HTTP API: validation, authentication and authorization, synchronous state changes, writing outbox rows |
| `relay` | `python -m pulseops.workers.relay` | Publishes committed outbox rows to SQS |
| `worker` | `python -m pulseops.workers.worker` | Consumes SQS: incident engine, investigations, simulated execution |
| `migrate` | `python -m pulseops.cli bootstrap` | One-shot job: Alembic migrations, queue creation, demo principals |
| `dashboard` | Vite (dev) / nginx (prod) | React single-page app that uses the API |

All of them share one codebase, the `pulseops` package, which is a modular monolith.

## Modules

```
pulseops/
  api/            HTTP layer only: routes, deps (auth/session), schemas, presenters, middleware, errors
  auth/           principals, roles -> permissions, API keys, session tokens
  audit/          append-only hash-chained audit records
  config/         pydantic-settings (PULSEOPS_*)
  db/             declarative base, engine/session helpers, model registry
  evidence/       evidence collection, redaction, full-text search, baselines
  incidents/      models, state machine, lifecycle service, engine (event -> incident)
  ingestion/      event contract, idempotent ingestion
  investigator/   provider interface, mock + Claude providers, tools, schema v1, validation, orchestration
  messaging/      outbox model/writer, queue port (SQS, in-memory)
  observability/  structured logging, correlation IDs, redaction, Prometheus metrics
  pipelines/      topology models, repository, dependency graph (cycle checks, recursive CTE traversal)
  remediation/    action allowlist + parameter/scope validation, proposals, approval, execution, sandbox port
  workers/        relay, worker loop, backoff, message handlers
simulator/        sandbox executor, scenario loader + fixtures, topology loader, evaluation, CLI
```

Dependency rules:

- Route handlers contain no business logic. They call domain services and commit.
- Domain services never import FastAPI.
- `remediation` reaches the simulator only through the `ExecutionSandbox` protocol (`remediation/port.py`). The concrete `SimulatorSandbox` is attached in `runtime.py`, which is the composition root.
- The investigator reaches models only through the `InvestigationProvider` protocol.

## Consistency guarantees and failure modes

| Concern | Mechanism | Guarantee |
|---|---|---|
| Event accepted but never processed | The event row and the outbox row are written in one transaction | An accepted (2xx) event always has a pending outbox message |
| Relay crashes after publishing, before commit | The row stays unpublished and is published again | At-least-once publication; duplicates are possible |
| Duplicate delivery from the producer | `UNIQUE (source, event_id)` + `ON CONFLICT DO NOTHING` | One stored event. Same payload returns 200; a different payload returns 409 |
| Duplicate queue delivery | The event row is locked with `FOR UPDATE`; `PROCESSED` is skipped | Processing is idempotent |
| Concurrent correlated events | Partial unique index on the active fingerprint, plus an insert-or-select | At most one active incident per fingerprint |
| Partial processing | One transaction per message; any exception rolls back everything | All or nothing per message |
| Transient failure | Exceptions trigger `ChangeMessageVisibility` with exponential backoff and jitter | Bounded retries (`max_receive_count`, default 5) |
| Poison message or exhausted retries | Explicit dead-letter (send to the DLQ, delete), or the broker's redrive policy | The message is kept in the DLQ; the aggregate is marked `DEAD_LETTERED` / `FAILED` |
| Worker killed mid-message | The visibility timeout expires and SQS redelivers | Verified on the local stack: the message was reprocessed 60 s later |
| Two approvers at once | Row lock on the proposal + `UNIQUE (approval_decision.proposal_id)` | Exactly one decision (concurrency test: 1 × 200, 5 × 409) |
| Duplicate execution | `UNIQUE (recovery_execution.proposal_id)` + a status check | A proposal executes at most once |
| Stale approval | Version and expiry check, with preconditions re-checked at approval and again at execution | No action runs against a changed incident |
| Lost updates | `Incident.version` is the SQLAlchemy `version_id_col` (optimistic locking) | Conflicting writers fail instead of overwriting each other |

Exactly-once processing is **not** claimed anywhere. Every consumer is written to tolerate redelivery.

## Investigation pipeline

1. **Claim.** `PENDING` → `RUNNING`. If the run is no longer `PENDING`, it is skipped, which makes redelivery idempotent.
2. **Collect** the standard evidence:
   - Each attached event and its log excerpt.
   - The last 10 executions of the task.
   - A runtime baseline (mean, stdev, p95, and the z-score of the latest run).
   - The status of each upstream task.
   - A downstream-impact summary.
   - Error and warning logs from a 6-hour window (full-text ranked).
3. **Investigate** with the provider:
   - **Mock:** calls the tools, then applies generic regex rules for connectivity, upstream failure, fixture corruption, OOM, latency, conflicting reports and injection.
   - **Claude:**
     - Uses a manual tool loop (at most 10 turns and 6 tool calls) with strict JSON-schema retrieval tools.
     - Returns its answer through a `submit_investigation` tool, whose output is validated with Pydantic in the loop. One repair turn is allowed.
     - Evidence is wrapped in escaped `<evidence>` blocks.
     - Uses `effort=medium`, adaptive thinking and `fallbacks: "default"`.
     - Maps errors by type: timeouts, 429, 5xx and connection errors are retryable; authentication errors, 400, refusals, truncation and malformed output are not.
4. **Validate and persist** in one transaction (see `investigator/validation.py`). The result, hypotheses, hypothesis↔evidence links, the exact set of evidence the run was shown (`investigation_evidence`) and an evidence hash are stored. If a valid action remains, a proposal is created and the incident moves to `ACTION_REQUIRED`; otherwise it moves back to `OPEN`.

Transactions are kept short. No row lock is held while a model call is in progress. Tool calls each open their own transaction.

## Observability

- **Logs.** JSON logs from structlog. Every line produced while handling a request or message carries its `correlation_id`. Secrets are redacted by a processor: key-name and pattern based, covering Anthropic keys, AWS key IDs, bearer tokens, `password=` and credentials in DSNs.
- **Metrics.** `/metrics` exposes request count and latency, events ingested, duplicates and rejections, outbox published and failures, messages processed and dead-lettered, investigations by provider and status with latency, unsupported claims, and recovery executions by action and status.
- **Health.** `/health` is liveness (no dependencies). `/ready` checks the database and the queue and returns 503 if either is down.
- **Operations.** `python -m pulseops.cli dead-letters` lists DLQ messages; `verify-audit` checks the audit hash chain.

## Known scalability limits

- Audit appends take a transaction-scoped advisory lock to keep the hash chain linear. This serialises audit writes.
- Evidence codes are allocated under the incident row lock.
- Rate limiting is in-process.

All three are appropriate for this workload and documented as limits rather than hidden.
