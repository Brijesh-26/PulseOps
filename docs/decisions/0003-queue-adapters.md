# ADR 0003: SQS with ElasticMQ locally and an in-memory adapter for tests

**Status:** accepted

**Decision.**
- Production uses SQS with a redrive policy to a DLQ.
- Local development uses ElasticMQ, which is SQS-compatible, so the same `SqsQueue` code runs, including visibility timeouts and redrive.
- Tests use `InMemoryQueue`, which has an injectable clock and simulates visibility and redrive deterministically.
- A small integration test checks `SqsQueue` against ElasticMQ.

**Rejected.**
- LocalStack: heavier than needed for SQS alone.
- A Postgres-backed queue: it would not demonstrate SQS semantics.
