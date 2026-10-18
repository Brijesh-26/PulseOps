"""SqsQueue against an SQS-compatible endpoint (ElasticMQ in docker compose / CI)."""

from __future__ import annotations

import os
import time
import uuid

import boto3
import pytest

from pulseops.messaging.queue import Envelope, SqsQueue

ENDPOINT = os.environ.get("PULSEOPS_TEST_SQS_ENDPOINT", "http://localhost:9324")

pytestmark = pytest.mark.sqs


@pytest.fixture
def sqs() -> SqsQueue:
    client = boto3.client(
        "sqs",
        region_name="elasticmq",
        endpoint_url=ENDPOINT,
        aws_access_key_id="x",
        aws_secret_access_key="x",  # ElasticMQ ignores credentials
    )
    try:
        client.list_queues()
    except Exception as exc:
        if "PULSEOPS_TEST_SQS_ENDPOINT" in os.environ:
            raise
        pytest.skip(f"SQS endpoint not available: {type(exc).__name__}")
    suffix = uuid.uuid4().hex[:8]
    q = SqsQueue(
        queue_name=f"test-work-{suffix}",
        dlq_name=f"test-dlq-{suffix}",
        region="elasticmq",
        endpoint_url=ENDPOINT,
        visibility_timeout=1,
        max_receive_count=2,
        client=client,
    )
    q.ensure_queues()
    return q


def _receive(q: SqsQueue, timeout: float = 5.0) -> list:  # type: ignore[type-arg]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msgs = q.receive(wait_seconds=1)
        if msgs:
            return msgs
    return []


def test_round_trip_and_ack(sqs: SqsQueue) -> None:
    sqs.check()
    sqs.send(Envelope(message_id="m1", type="t", payload={"a": 1}, correlation_id="cid-12345678"))
    [msg] = _receive(sqs)
    assert msg.envelope.payload == {"a": 1}
    assert msg.envelope.correlation_id == "cid-12345678"
    assert msg.receive_count == 1
    sqs.ack(msg)
    time.sleep(1.5)
    assert sqs.receive(wait_seconds=1) == []


def test_unacked_message_is_redelivered_then_redriven_to_dlq(sqs: SqsQueue) -> None:
    sqs.send(Envelope(message_id="m2", type="t", payload={}))
    first = _receive(sqs)
    assert first[0].receive_count == 1
    second = _receive(sqs)  # visibility timeout (1s) expired -> redelivered
    assert second[0].receive_count == 2
    # A third receive exceeds maxReceiveCount=2: the broker moves it to the DLQ.
    assert _receive(sqs, timeout=4) == []
    dead = []
    for _ in range(5):
        dead = sqs.list_dead_letters()
        if dead:
            break
        time.sleep(1)
    assert [d.message_id for d in dead] == ["m2"]


def test_explicit_dead_letter(sqs: SqsQueue) -> None:
    sqs.send(Envelope(message_id="m3", type="t", payload={}))
    [msg] = _receive(sqs)
    sqs.dead_letter(msg, "permanent: bad payload")
    assert [d.message_id for d in sqs.list_dead_letters()] == ["m3"]
