# PulseOps

PulseOps investigates data pipeline failures. It reads logs, task history and dependency information, then works out an evidence-backed root cause and maps the downstream impact. It can propose a recovery action. That action runs only in a simulator, and only after a human approves it.

It is a working backend system, not a chatbot wrapper. The pipeline is:

1. **Ingestion.** Events are ingested idempotently.
2. **Outbox and queue.** A transactional outbox hands work to SQS, with a dead-letter queue.
3. **Incident engine.** An explicit state machine tracks each incident.
4. **AI investigation.** The investigator uses typed retrieval tools and returns a versioned, strict schema.
5. **Backend validation.** Every claim must cite stored evidence.
6. **Approval and execution.** An allowlisted action runs in the simulator only after a separate person approves it.
7. **Audit trail.** Every step is recorded in an append-only, hash-chained audit trail.

> Status: a portfolio project. It runs end to end locally, and every number in this README comes from real runs. It is **not production-ready**. See [Limitations](#limitations) and [docs/security.md](docs/security.md).

![Incident detail: upstream dependency failure](docs/screenshots/incident-upstream.png)

## Contents
- [The problem](#the-problem)
- [How it works](#how-it-works)
- [Design decisions](#design-decisions)
- [Technology](#technology)
- [Running locally](#running-locally)
- [Failure scenarios](#failure-scenarios)
- [API](#api)
- [Evaluation results](#evaluation-results)
- [Security model](#security-model)
- [Testing](#testing)
- [Deployment](#deployment)
- [Limitations](#limitations)
- [Future work](#future-work)

## The problem

When a pipeline fails, an on-call data engineer has to do several things:

- Read the failing task's logs.
- Check whether earlier runs succeeded.
- Find out what is upstream and what is downstream.
- Decide which datasets can no longer be trusted.
- Decide whether a retry is safe.

That work is repetitive and urgent, and mistakes are costly. An LLM can speed it up, but only with guardrails. It must not invent evidence, act on instructions hidden in logs, or touch infrastructure. PulseOps shows one way to combine deterministic engineering with AI-assisted investigation under those constraints.

## How it works

```mermaid
flowchart LR
    P[Pipeline / simulator] -->|POST /api/v1/events<br/>API key, validated| API
    subgraph API process
      API[FastAPI] -->|1 tx: event + outbox row| DB[(PostgreSQL)]
    end
    DB --> R[Outbox relay<br/>SKIP LOCKED] -->|publish| Q[[SQS work queue]]
    Q -->|redrive after N receives| DLQ[[SQS DLQ]]
    Q --> W[Worker]
    W -->|incident engine<br/>state machine| DB
    W -->|investigation| INV[Investigator]
    INV -->|typed tools: logs, history,<br/>dependencies, baselines| DB
    INV -->|provider interface| LLM{{Mock rules or<br/>Claude API}}
    INV -->|schema + evidence validation| DB
    DB -->|pending proposal| UI[React dashboard]
    UI -->|approve / reject<br/>approver role, 4-eyes| API
    API -->|outbox: proposal.approved| R
    W -->|allowlisted action,<br/>re-checked preconditions| SIM[Simulator sandbox]
    SIM --> DB
```

An event moves through the system like this:

1. **Ingest.** `POST /api/v1/events` authenticates the caller and validates the event: schema, size limits, clock skew and known pipeline/task. In one transaction it inserts the event (unique on `source + event_id`) and an outbox row. A duplicate delivery returns the original record with `200`. If the same `event_id` arrives with a different payload, the API returns `409`.
2. **Relay.** The outbox relay claims rows with `FOR UPDATE SKIP LOCKED` and publishes them to SQS. In development that is ElasticMQ; in tests it is a deterministic in-memory adapter.
3. **Process.** The worker processes each message in a single transaction:
   - It records the task execution.
   - It upserts the incident, keyed by a fingerprint. A partial unique index allows one *active* incident per fingerprint, so correlated downstream `DEPENDENCY_FAILED` events join the upstream incident.
   - It attaches evidence and requests an investigation through the outbox.

   Failures retry with exponential backoff and jitter. After the attempts run out, the message is dead-lettered and the event is marked `DEAD_LETTERED`.
4. **Investigate.** The investigator collects evidence:
   - Event payloads and log excerpts.
   - Execution history.
   - Runtime baselines.
   - Upstream status and the downstream graph.

   Each item gets a short code (`E1`, `E2` and so on). The provider can call typed, read-only tools that are scoped to the incident's dependency neighbourhood. It must return `InvestigationResultV1`.
5. **Validate.** The backend:
   - Strips citations to evidence codes that do not exist and counts them as unsupported claims.
   - Caps the confidence of unsupported hypotheses.
   - Replaces claimed impact with impact computed from the graph.
   - Flags text that claims an action was executed.
   - Independently flags instruction-like evidence, which can never be used as support.
   - Accepts a proposed action only if it is allowlisted, valid for this incident's scope, backed by evidence, and linked to a supported hypothesis with confidence ≥ 0.6.
6. **Approve.** A human with the `approver` role approves or rejects. A proposer can never approve their own proposal. The AI identity holds no roles and cannot authenticate.
   - Approval is refused if the proposal has expired.
   - It is refused if the incident changed after the proposal was made (version check).
   - It is refused if the preconditions no longer hold.
7. **Execute.** The worker runs the action in the simulator exactly once: `recovery_execution.proposal_id` is unique. It re-checks preconditions first and verifies postconditions afterwards. A resolving action moves the incident to `RESOLVED`. A diagnostic action adds evidence and returns the incident to `OPEN` for re-investigation.

Incident states and the server-enforced transitions:

```mermaid
stateDiagram-v2
    [*] --> OPEN
    OPEN --> INVESTIGATING: investigation requested
    INVESTIGATING --> ACTION_REQUIRED: valid proposal
    INVESTIGATING --> OPEN: no justified action / failed
    OPEN --> ACTION_REQUIRED: human proposal
    ACTION_REQUIRED --> RECOVERING: approved
    ACTION_REQUIRED --> OPEN: rejected / stale
    ACTION_REQUIRED --> INVESTIGATING: re-investigate
    RECOVERING --> RESOLVED: resolving action succeeded
    RECOVERING --> OPEN: diagnostic done / failed
    OPEN --> RESOLVED: manual
    OPEN --> CLOSED: manual
    RESOLVED --> CLOSED: manual
    RESOLVED --> OPEN: reopen
    CLOSED --> [*]
```

There is more detail in [docs/architecture.md](docs/architecture.md) and [docs/data-model.md](docs/data-model.md).

## Design decisions

Each decision has a short ADR in [docs/decisions/](docs/decisions).

| Decision | Why |
|---|---|
| Modular monolith (`pulseops/<domain>`) with separate API, worker and relay processes | Clear module boundaries without the operational cost of microservices. |
| Transactional outbox and at-least-once delivery, with idempotent consumers | An accepted event is never lost between the DB commit and the queue publish. Exactly-once delivery is **not** claimed. |
| SQS + DLQ, ElasticMQ locally, in-memory adapter for tests | Real queue semantics (visibility timeout, redrive) locally at no cost. Tests stay deterministic. |
| Synchronous SQLAlchemy 2 + psycopg 3 | Simpler transaction reasoning. Endpoints are short and I/O-bound on Postgres. |
| Postgres full-text search, no vector database | Evidence volume per incident is small. Keyword search over logs is sufficient and explainable. |
| Evidence codes as the only citation mechanism | Every conclusion can be traced to stored, immutable rows. Fabricated citations are mechanically detectable. |
| The model proposes, the backend decides | Allowlist, scope validation, confidence threshold, human approval and precondition checks are all deterministic code. |
| Hash-chained, append-only audit with DB triggers and a least-privilege app role | Tampering is blocked for the app role and detectable for anyone who bypasses the triggers. |

## Technology

- **Backend:**
  - Python 3.12 and FastAPI 0.143
  - Pydantic 2.14 and SQLAlchemy 2.1
  - Alembic, psycopg 3 and boto3
  - structlog and prometheus-client
  - Anthropic SDK 1.15
- **Data and queue:** PostgreSQL 17, AWS SQS (ElasticMQ 1.6 locally)
- **AI:** a provider-neutral interface with two implementations:
  - A deterministic rule-based mock provider (the default, offline and free).
  - A Claude provider: `claude-opus-5-5` with adaptive thinking, a bounded tool loop and a server-side refusal fallback.
- **Frontend:**
  - React 19, TypeScript 6 and Vite 8
  - TanStack Query 5
  - Vitest and Testing Library
- **Tooling:**
  - uv, Ruff and mypy (strict)
  - pytest
  - Docker Compose
  - GitHub Actions
  - pip-audit, npm audit and gitleaks

## Running locally

Prerequisites are Docker and, for running backend tests on the host, [uv](https://docs.astral.sh/uv/). Node.js is **not** required on the host because the dashboard tooling runs in containers.

```bash
cp .env.example .env          # optional: placeholders only; the defaults work for local use
make up                       # postgres, elasticmq, migrate+bootstrap, api, relay, worker, dashboard
curl localhost:8000/ready     # {"status":"ready","checks":{"database":"ok","queue":"ok"}}
make scenarios                # load all five failure scenarios
open http://localhost:5173    # sign in with a demo key (below)
```

`make up` runs the `migrate` service, which:

- applies the Alembic migrations (`python -m pulseops.cli bootstrap`),
- creates the SQS work queue and DLQ with a redrive policy, and
- seeds demo principals when `PULSEOPS_DEMO_MODE=true`.

**Local demo keys.** These are for local use only. They are set in `docker-compose.yml` and can be overridden through `PULSEOPS_DEMO_KEY_*`.

| Principal | Role | Key |
|---|---|---|
| demo-admin | admin | `po_demo_admin_local_only_00001` |
| demo-operator | operator (investigate, propose) | `po_demo_operator_local_only_01` |
| demo-approver | approver (decide) | `po_demo_approver_local_only_01` |
| demo-approver-2 | approver | `po_demo_approver2_local_only_1` |
| demo-viewer | viewer | `po_demo_viewer_local_only_0001` |
| demo-ingest | ingest | `po_demo_ingest_local_only_0001` |

For production, create real keys with `python -m pulseops.cli create-key <name> --role approver`. Each key is shown once and stored as a hash.

**Environment variables** are listed in [.env.example](.env.example) and in `pulseops/config/settings.py`. The important ones are:

| Variable | Default | Purpose |
|---|---|---|
| `PULSEOPS_DATABASE_URL` | app role @ localhost:5433 | Runtime DB (least-privilege role) |
| `PULSEOPS_MIGRATION_DATABASE_URL` | — | Owner role for migrations |
| `PULSEOPS_QUEUE_BACKEND` | `sqs` | `sqs` or `memory` (tests) |
| `PULSEOPS_SQS_ENDPOINT_URL` | — | ElasticMQ locally; unset on AWS |
| `PULSEOPS_INVESTIGATOR_PROVIDER` | `mock` | `mock` (free, offline) or `anthropic` (paid) |
| `PULSEOPS_ANTHROPIC_API_KEY` | — | Needed only for `anthropic` |
| `PULSEOPS_ANTHROPIC_MODEL` / `_EFFORT` | `claude-opus-5-5` / `medium` | Claude model and effort |
| `PULSEOPS_SESSION_SECRET` | dev-only fallback | ≥ 32 chars, required in prod |
| `PULSEOPS_DEMO_MODE` | `false` (compose sets `true`) | Enables the admin-only scenario loader; refused in prod |
| `PULSEOPS_ACTION_MIN_CONFIDENCE` | `0.6` | Threshold for accepting a proposed action |

**Migrations:**

```bash
docker compose run --rm migrate python -m pulseops.cli migrate
# or on the host:
PULSEOPS_MIGRATION_DATABASE_URL=postgresql+psycopg://pulseops:pulseops@localhost:5433/pulseops uv run alembic upgrade head
```

Common problems are covered in [docs/development.md](docs/development.md#troubleshooting).

## Failure scenarios

Each scenario is a YAML fixture in [`simulator/scenarios/`](simulator/scenarios). A fixture contains the topology, simulated resources, execution history, logs, the events to deliver, and the **expected** outcome. Load one in either of these ways:

- **Running stack:** `make scenario-a` (calls the admin-only `POST /api/v1/demo/scenarios/a`).
- **As an external producer:**

  ```bash
  PULSEOPS_INGEST_KEY=... uv run python -m simulator run a
  ```

  This seeds the orchestrator state and POSTs the events over HTTP.

| | Scenario | What happens | Expected outcome |
|---|---|---|---|
| A | Database connection failure | The warehouse load fails three times with connection timeouts. The outage was transient. | `DATABASE_CONNECTIVITY`. A read-only connectivity check is proposed, then (after re-investigation) a single bounded retry, which resolves the incident. |
| B | Slow pipeline | A model build takes 40 minutes against a baseline of about 10. The logs are routine. | The runtime regression is stated as a fact, but **no cause is invented**: `UNKNOWN`, confidence ≤ 0.3, missing information listed, no action. |
| C | Upstream dependency failure | A source API returns 503, and two of four downstream tasks report `DEPENDENCY_FAILED`. | One incident. The dependency paths to all four affected tasks are computed. `MARK_DATASET_STALE` is proposed for the five affected datasets. |
| D | Duplicate delivery | The same failure event is delivered three times. | One stored event, one incident, one investigation. After concurrent approvals and redelivered approval messages, exactly one execution. |
| E | Misleading evidence | Contradictory logs (a timeout, then "completed successfully"), an irrelevant line, and a prompt injection in the log excerpt. | Injection flagged as suspicious and never used as support. Low confidence, missing information listed, **no action**. |

Automated assertions:

- [`tests/evaluation/test_scenarios.py`](tests/evaluation/test_scenarios.py) compares each scenario with its expected cause and evidence.
- [`tests/integration/test_workflow.py`](tests/integration/test_workflow.py) checks the approval and execution behaviour.

## API

The interactive OpenAPI docs are served at `http://localhost:8000/docs`.

| Method | Path | Permission |
|---|---|---|
| POST | `/api/v1/events` | `events:write` (ingest) |
| GET | `/api/v1/events` | `incidents:read` |
| GET | `/api/v1/incidents` | `incidents:read`. Filters: `status` (multi), `severity` (multi), `category`, `pipeline`, `q`, `opened_after`, `sort`, `limit`, `offset` |
| GET | `/api/v1/incidents/{id}` | `incidents:read` |
| GET | `/api/v1/incidents/{id}/evidence?q=&kind=` | `incidents:read` (full-text search) |
| GET | `/api/v1/incidents/{id}/timeline` | `incidents:read` |
| GET | `/api/v1/incidents/{id}/impact` | `incidents:read` |
| GET | `/api/v1/incidents/{id}/investigations` | `incidents:read` |
| POST | `/api/v1/incidents/{id}/investigate` | `investigations:run` (operator) |
| GET / POST | `/api/v1/incidents/{id}/proposals` | read / `proposals:create` (operator) |
| POST | `/api/v1/incidents/{id}/transitions` | `incidents:transition` (manual resolve/close/reopen only) |
| GET | `/api/v1/proposals/{id}` | `incidents:read` |
| POST | `/api/v1/proposals/{id}/approve` | `proposals:decide` (approver, not the proposer) |
| POST | `/api/v1/proposals/{id}/reject` | `proposals:decide`, reason required |
| GET | `/api/v1/audit`, `/api/v1/audit/verify` | `audit:read` |
| GET | `/api/v1/stats` | `incidents:read` |
| POST | `/api/v1/auth/session`, GET `/api/v1/auth/me` | API key → short-lived token |
| GET / POST | `/api/v1/demo/scenarios[/{key}]` | `demo:admin`, and only when demo mode is on (otherwise 404) |
| GET | `/health`, `/ready`, `/metrics` | public (`/metrics` is blocked at the proxy in prod) |

These requests and responses were captured from the running stack:

```text
$ curl -X POST localhost:8000/api/v1/events -H "X-Api-Key: $INGEST" -d '{"event_id":"readme-001","source":"airflow",
    "pipeline_id":"a_orders_etl","task_id":"load_warehouse","event_type":"TASK_FAILED","severity":"HIGH",
    "occurred_at":"2026-10-18T09:32:28Z","log_excerpt":"psycopg.OperationalError: connection to a-warehouse-db timed out"}'
HTTP/1.1 202 Accepted
{"id":"3b26646d-3ae4-44c7-bb77-cd6fdad54151","event_id":"readme-001","source":"airflow","status":"RECEIVED",
 "duplicate":false,"received_at":"2026-10-18T09:32:28.472422Z","correlation_id":"5731206a7ea94fccb75b81421e4eb794","incident_id":null}

# same request again
HTTP 200
{"id":"3b26646d-3ae4-44c7-bb77-cd6fdad54151",...,"duplicate":true,...}

# invalid event_type
HTTP 422
{"error":{"code":"validation_error","message":"Request validation failed","details":[{"loc":["body","event_type"],
 "msg":"Input should be 'TASK_FAILED', 'TASK_SUCCEEDED', 'LATENCY_ANOMALY' or 'DEPENDENCY_FAILED'","type":"enum"}],
 "correlation_id":"2bb942c2afd84654997d616b397e7feb"}}

# a viewer trying to approve
HTTP 403
{"error":{"code":"forbidden","message":"Principal lacks permission proposals:decide","details":{"required":"proposals:decide"},
 "correlation_id":"4b4821e355f14908a6e2370dded26c1e"}}
```

Every error uses the same `{"error": {code, message, details, correlation_id}}` shape. The correlation ID is returned in `X-Correlation-Id`. It is propagated through the outbox and SQS message attributes into worker logs.

## Evaluation results

`make evaluate` (`python -m simulator evaluate`) runs every scenario on a freshly migrated database and writes `reports/evaluation-<provider>.{json,md}`. The latest run used the **mock provider**:

| Metric | Value |
|---|---|
| Scenarios passing all checks | 5 / 5 |
| Root-cause accuracy (4 scenarios have an expected category) | 4 / 4 |
| Proposed-action accuracy | 5 / 5 |
| Unjustified actions | 0 |
| Invalid evidence citations in stored results | 0 |

**Read these numbers carefully.** The mock provider is a deterministic rule engine that I wrote alongside the scenarios. A perfect score therefore shows that the pipeline works and the guardrails hold: evidence linking, validation, impact computation, idempotency, uncertainty handling and approval gating. It says **nothing about LLM reasoning quality**.

- The Claude provider is implemented and covered by tests with a fake client. **No evaluation against the real Claude API has been run**, so no accuracy figure exists for it. Run `uv run python -m simulator evaluate --provider anthropic` with a key to produce one.
- The validator's unsupported-claim detection is measured separately, with adversarial scripted providers in [`tests/integration/test_investigation.py`](tests/integration/test_investigation.py). Those tests cover fabricated evidence IDs, out-of-scope actions, low-confidence actions, fake impact claims, execution claims, malformed output, and a provider that obeys an injection.

The full report is in [docs/evaluation.md](docs/evaluation.md).

## Security model

[docs/security.md](docs/security.md) covers this in full: what is implemented and what is not production-grade. In summary:

- **Authentication.** Hashed API keys (SHA-256 of 256-bit random keys) are exchanged for 1-hour HMAC-signed session tokens. The principal is re-checked on every request, so deactivation takes effect immediately. This is **demo auth**: there is no SSO, MFA or key rotation workflow.
- **Authorization.** Role-based permissions: ingest, viewer, operator, approver and admin. Operators cannot approve, approvers cannot propose, nobody can approve their own proposal, and the AI identity holds no roles.
- **AI safety controls:**
  - Logs are treated as untrusted data in delimited, escaped blocks.
  - Injection detection runs in the backend, independent of the model.
  - Tools are scoped to the incident.
  - Secrets are redacted before evidence is stored or sent.
  - Output uses a strict schema and is validated by the backend.
  - The action allowlist and parameter scope checks are deterministic.
  - No shell, network or subprocess access exists in the execution path; actions only change simulator tables.
- **Data and transport protections:**
  - Parameterised ORM queries; LIKE patterns are escaped.
  - Body-size limits (API and proxy).
  - Strict CORS.
  - Per-process token-bucket rate limits on ingestion and auth.
  - Security headers and CSP in prod.
- **Audit.** An append-only `audit_record` table:
  - Triggers reject UPDATE, DELETE and TRUNCATE for every role.
  - The app role has no UPDATE or DELETE grant.
  - A SHA-256 hash chain is verifiable through `GET /api/v1/audit/verify`, and a test proves it detects tampering.

## Testing

```bash
docker compose up -d postgres elasticmq
uv run pytest -q          # 122 tests: unit, integration (real Postgres + ElasticMQ), evaluation
make dashboard-check      # dashboard: eslint, tsc, 10 vitest tests, production build
make lint typecheck       # ruff + mypy --strict
```

Coverage at the time of writing was 89% (`pytest --cov=pulseops --cov=simulator`). CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs:

- lint, type checks and all tests against Postgres and ElasticMQ service containers,
- the evaluation report (uploaded as an artifact),
- the dashboard checks,
- Docker image builds,
- pip-audit, npm audit, and
- a gitleaks scan of the full history.

The CI workflow has been written but **has not yet run on GitHub**, because the repository has not been pushed. Each step was run locally.

## Deployment

The recommended low-cost target is **a single small VM** running Docker Compose, with Caddy for automatic TLS, real AWS SQS and a DLQ, and Postgres on the VM. Exact steps and cost drivers are in [docs/deployment.md](docs/deployment.md), and the configuration is in [deploy/vm/](deploy/vm).

**No deployment has been performed.** No cloud credentials were available, so there is no public demo URL.

## Limitations

- **Simulated recovery.** Recovery actions run against a simulator, and orchestrator metadata is seeded by fixtures rather than read from Airflow or MWAA. An adapter is the obvious next step.
- **Demo-grade auth.** Session tokens live in browser `sessionStorage`, which XSS could read.
- **Per-process rate limits.** They are not shared across replicas.
- **Serialised audit writes.** Audit appends take a global advisory lock, which limits audit throughput. That is fine for this workload but would not scale to very high write rates.
- **Confidence is uncalibrated.** The mock evaluation does not measure LLM quality (see above).
- **Single database.** Postgres runs on a single VM with no automated backups in the provided config. Use RDS or a backup job for anything real.
- **No real alerting.** There is no notification channel or paging integration.

## Future work

- An Airflow/MWAA metadata adapter, and a real-but-safe action executor behind the same `ExecutionSandbox` interface.
- An evaluation set run against the real Claude provider, with confidence-calibration analysis.
- OIDC sign-in, and keys stored in a secrets manager.
- OpenTelemetry tracing across API, relay and worker.
- Slack or Teams notifications for pending approvals.

## License

[MIT](LICENSE). Anyone may use, copy, modify and distribute the code, including commercially, as long as the copyright notice is kept. There is no warranty.
