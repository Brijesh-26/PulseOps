"""Prometheus metrics exposed at /metrics."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter(
    "pulseops_http_requests_total", "HTTP requests", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "pulseops_http_request_duration_seconds", "HTTP request latency", ["method", "route"]
)
EVENTS_INGESTED = Counter(
    "pulseops_events_ingested_total", "Events accepted by the ingestion API", ["event_type"]
)
EVENTS_DUPLICATE = Counter(
    "pulseops_events_duplicate_total", "Duplicate event deliveries detected at ingestion"
)
EVENTS_REJECTED = Counter(
    "pulseops_events_rejected_total", "Events rejected at ingestion", ["reason"]
)
OUTBOX_PUBLISHED = Counter("pulseops_outbox_published_total", "Outbox messages published")
OUTBOX_PUBLISH_FAILURES = Counter(
    "pulseops_outbox_publish_failures_total", "Outbox publish failures"
)
MESSAGES_PROCESSED = Counter(
    "pulseops_messages_processed_total", "Queue messages processed", ["message_type", "outcome"]
)
MESSAGES_DEAD_LETTERED = Counter(
    "pulseops_messages_dead_lettered_total", "Queue messages sent to the DLQ", ["message_type"]
)
INVESTIGATIONS = Counter(
    "pulseops_investigations_total", "Investigation runs", ["provider", "status"]
)
INVESTIGATION_LATENCY = Histogram(
    "pulseops_investigation_duration_seconds", "Investigation run latency", ["provider"]
)
UNSUPPORTED_CLAIMS = Counter(
    "pulseops_investigation_unsupported_claims_total",
    "Claims in model output that failed evidence validation",
)
RECOVERY_EXECUTIONS = Counter(
    "pulseops_recovery_executions_total", "Simulated recovery executions", ["action", "status"]
)
