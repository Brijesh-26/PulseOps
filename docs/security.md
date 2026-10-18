# Security

This document separates what is implemented and tested from what would be required in production. Being able to run the project does not make it production-ready.

## Implemented

### Identity and access (demo-grade)
- **API keys.**
  - Keys are `po_<kind>_<43 chars>` (256 bits of randomness), stored only as SHA-256 hashes. Because the keys are high-entropy random values, a fast hash is appropriate; no password stretching is needed.
  - New keys are shown once (`python -m pulseops.cli create-key`).
  - Local demo keys are fixed strings defined in `docker-compose.yml` for convenience. **Never reuse them.**
- **Session tokens.**
  - `POST /api/v1/auth/session` exchanges an API key for an HMAC-SHA256-signed token with a one-hour expiry.
  - The signature is compared in constant time.
  - The principal is reloaded on every request, so deactivating it revokes access immediately (tested).
- **Roles and permissions** (`auth/principal.py`):

  | Role | Permissions |
  |---|---|
  | ingest | `events:write` |
  | viewer | read incidents, read audit |
  | operator | read, investigate, create proposals, manual transitions |
  | approver | read, decide proposals |
  | admin | all, including the demo scenario loader |

- **Separation of duties.**
  - Operators cannot approve, and approvers cannot propose.
  - The proposer can never decide their own proposal (`self_approval_forbidden`).
  - AI proposals are attributed to `system:investigator`, which has no key and no roles.
- **Failed logins** are audited, with the key prefix only.
- **Brute force.** Authentication endpoints are rate-limited per client IP.

### AI-specific controls
- **Untrusted input.** Logs and payloads are treated as untrusted data:
  - They are rendered inside escaped `<evidence>` blocks, so `<` and `>` are entity-encoded and the delimiters cannot be forged.
  - The system prompt forbids following instructions found in evidence.
- **Injection detection** (`investigator/safety.py`) runs in the backend, independent of the model. Matching evidence cannot support a hypothesis or an action, even if the model did not flag it. This is a heuristic, not a complete defence, so the controls below do not rely on it.
- **Backend validation** of output (`investigator/validation.py`):
  - Strict schema with `extra="forbid"`; `incident_id` must match.
  - Unknown evidence codes are stripped and counted.
  - Unsupported hypotheses have their confidence capped.
  - Impact is replaced with graph-computed impact.
  - Execution claims are flagged.
  - Proposed actions face an allowlist, parameter-model and scope check, an evidence requirement, and a confidence threshold.
- **No arbitrary execution.**
  - Actions are a fixed enum dispatched to fixed functions.
  - Parameters can only reference the incident's own task, its dependency neighbourhood, its resources and its datasets.
  - The sandbox has no shell, subprocess or network calls; it only modifies simulator tables.
- **Tool scope.** Tools are read-only, limited to related tasks, size-capped, and capped at 6 calls per run. Every result is persisted as evidence before the model sees it.
- **Data minimisation.**
  - Only redacted, size-capped evidence is sent to the model; no principal names, keys or connection strings.
  - Redaction runs both when evidence is stored and in the logging pipeline.
- **Honest failure reporting.** Missing credentials, refusals, timeouts and malformed output are recorded as typed run failures; the system never fakes a success.

### Application and data
- **SQL injection.** All queries use the SQLAlchemy ORM or Core with bound parameters. Raw SQL is limited to constant DDL in migrations and parameterised CTEs. The incident search escapes LIKE metacharacters, and a test sends `%' OR 1=1--`.
- **Input validation.**
  - Every request body is a Pydantic model with `extra="forbid"`.
  - Event limits: metadata ≤ 50 keys and 8 KB, log excerpt ≤ 8000 characters, clock skew ≤ 5 minutes, events ≤ 30 days old.
  - NUL bytes are stripped.
- **Body size.** An ASGI middleware enforces 64 KB using both Content-Length and the streamed body; in prod, Caddy enforces 128 KB.
- **CORS.** An explicit origin list (the dashboard only), and only `GET`/`POST` with listed headers. Wildcard origins are rejected in prod settings.
- **Rate limits.** A token bucket on ingestion and authentication.
- **Errors.** One consistent schema; unhandled errors return a generic 500 with a correlation ID, and stack traces stay in server logs.
- **Audit trail.**
  - Records security-sensitive operations: logins, investigation requests and results, proposal creation and decisions, staleness, executions, state transitions and demo scenario loads.
  - Append-only. Triggers reject UPDATE, DELETE and TRUNCATE for every role, including the owner.
  - The runtime role `pulseops_app` has no UPDATE, DELETE or TRUNCATE grant on the table. The test runs as `pulseops_app` and asserts that UPDATE is rejected with "permission denied".
  - The SHA-256 hash chain detects tampering by anyone who bypasses the triggers, such as a superuser; this is also tested.
- **Least-privilege database role.** Migrations run as the owner and the app runs as `pulseops_app`.
- **Configuration and secrets.**
  - Configuration comes from the environment. `SecretStr` keeps secrets out of logs.
  - `.env*` files are gitignored, and `.env.example` contains placeholders only.
  - Prod settings require a session secret of at least 32 characters and refuse demo mode.
- **Demo endpoint.** The scenario loader is admin-only, returns 404 unless demo mode is on, and demo mode is refused when `environment=prod`.
- **Dashboard.**
  - React escapes all evidence text, and an ESLint rule forbids `dangerouslySetInnerHTML`; a test checks that a `<script>` in a log is rendered as text.
  - A strict CSP and security headers are applied in prod (nginx/Caddy).
- **Supply chain.**
  - Dependencies are pinned through `uv.lock` and `package-lock.json`.
  - CI runs pip-audit, npm audit and a gitleaks scan of the full history.
  - Locally on 2026-10-18 these reported no known vulnerabilities and no leaks.
- **Containers.** The API image runs as a non-root user.

## Not production-grade

- **No real identity provider.** There is no SSO, OIDC, MFA, key rotation or expiry policy for API keys.
- **Session token storage.** The dashboard keeps the bearer token in `sessionStorage`, which XSS could read. Production should use HttpOnly SameSite cookies with CSRF protection, or an OIDC flow.
- **Rate limits** are per process and per client IP, not shared; a gateway or Redis is needed.
- **Injection detection** is pattern-based. A sufficiently creative injection may not be flagged. What protects the system is that actions are allowlisted, scoped, threshold-gated and **always** require human approval.
- **Audit integrity** is application-level. The hash chain is not anchored externally, for example by periodically publishing the head hash to WORM storage, so a superuser who rewrites the whole chain consistently would not be detected.
- **Transport.** TLS terminates at Caddy, and the database connection inside the VM is not encrypted.
- **Backups and disaster recovery.** None are configured in the VM deployment.
- **Not done:** penetration testing, threat-model review and dependency licence review.
