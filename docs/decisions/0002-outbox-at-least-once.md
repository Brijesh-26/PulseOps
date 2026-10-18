# ADR 0002: Transactional outbox and at-least-once delivery

**Status:** accepted

**Context.** Writing an event to Postgres and publishing it to SQS cannot be made atomic. Publishing first can lose data on rollback; committing first can lose the publish if the process crashes.

**Decision.**
- The event and an `outbox_message` row are written in the same transaction.
- A relay publishes committed rows (`FOR UPDATE SKIP LOCKED`) and marks them published.
- Consumers are idempotent: row locks plus status checks, unique constraints on incident fingerprint, decision and execution, and `ON CONFLICT` upserts.

**Consequences.**
- No accepted event is lost.
- Duplicates are possible and handled.
- Exactly-once delivery is explicitly not claimed.
- There is an extra process and a short polling delay of about 1 s.
