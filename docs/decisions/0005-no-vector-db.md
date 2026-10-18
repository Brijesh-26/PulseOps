# ADR 0005: Postgres full-text search instead of a vector database

**Status:** accepted

**Decision.** Use a generated `tsvector` column with a GIN index on logs and evidence, queried with `websearch_to_tsquery`.

**Reasoning.**
- Evidence per incident is small, a few dozen items, and is mostly keyword-identifiable error text.
- Results are explainable and need no extra infrastructure.

**Revisit** when there is a demonstrated need, such as semantic retrieval across historical incidents.
