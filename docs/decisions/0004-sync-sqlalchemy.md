# ADR 0004: Synchronous SQLAlchemy with psycopg 3

**Status:** accepted

**Decision.** Use synchronous sessions. FastAPI runs synchronous endpoints in a threadpool.

**Reasoning.**
- The workload is I/O-bound on Postgres with short transactions.
- Synchronous code keeps transaction boundaries, row locks and savepoints easy to reason about.
- LLM calls happen in the worker, outside any transaction.

**Revisit** if request concurrency per instance becomes a bottleneck.
