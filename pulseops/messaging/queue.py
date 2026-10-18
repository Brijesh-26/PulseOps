"""Queue port with an SQS adapter and a deterministic in-memory adapter.

Delivery semantics are at-least-once for both adapters: a message that is
received but not acknowledged before its visibility timeout becomes visible
again. Consumers must be idempotent.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field


class Envelope(BaseModel):
    """Wire format for every queue message."""

    message_id: str
    type: str
    payload: dict[str, Any]
    correlation_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class ReceivedMessage:
    envelope: Envelope
    receive_count: int
    receipt: str


class QueuePort(Protocol):
    def send(self, envelope: Envelope, delay_seconds: int = 0) -> None: ...

    def receive(self, max_messages: int = 10, wait_seconds: int = 0) -> list[ReceivedMessage]: ...

    def ack(self, message: ReceivedMessage) -> None: ...

    def retry_later(self, message: ReceivedMessage, delay_seconds: int) -> None: ...

    def dead_letter(self, message: ReceivedMessage, reason: str) -> None: ...

    def list_dead_letters(self, limit: int = 10) -> list[Envelope]: ...

    def check(self) -> None: ...


# --------------------------------------------------------------------------- in-memory


@dataclass
class _Stored:
    envelope: Envelope
    visible_at: float
    receive_count: int = 0
    receipt: str | None = None
    dlq_reason: str | None = None


@dataclass
class InMemoryQueue:
    """Deterministic SQS-like queue for tests (injectable clock, simulated redrive)."""

    max_receive_count: int = 5
    visibility_timeout: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _messages: list[_Stored] = field(default_factory=list)
    _dlq: list[_Stored] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def send(self, envelope: Envelope, delay_seconds: int = 0) -> None:
        with self._lock:
            self._messages.append(_Stored(envelope, self.clock() + delay_seconds))

    def receive(self, max_messages: int = 10, wait_seconds: int = 0) -> list[ReceivedMessage]:
        now = self.clock()
        out: list[ReceivedMessage] = []
        with self._lock:
            for stored in list(self._messages):
                if len(out) >= max_messages:
                    break
                if stored.visible_at > now:
                    continue
                if stored.receive_count >= self.max_receive_count:
                    # Mirrors SQS redrive: moved to the DLQ instead of being delivered again.
                    self._messages.remove(stored)
                    stored.dlq_reason = "max_receive_count_exceeded"
                    self._dlq.append(stored)
                    continue
                stored.receive_count += 1
                stored.receipt = uuid.uuid4().hex
                stored.visible_at = now + self.visibility_timeout
                out.append(ReceivedMessage(stored.envelope, stored.receive_count, stored.receipt))
        return out

    def _find(self, message: ReceivedMessage) -> _Stored | None:
        return next((m for m in self._messages if m.receipt == message.receipt), None)

    def ack(self, message: ReceivedMessage) -> None:
        with self._lock:
            stored = self._find(message)
            if stored is not None:
                self._messages.remove(stored)

    def retry_later(self, message: ReceivedMessage, delay_seconds: int) -> None:
        with self._lock:
            stored = self._find(message)
            if stored is not None:
                stored.visible_at = self.clock() + delay_seconds

    def dead_letter(self, message: ReceivedMessage, reason: str) -> None:
        with self._lock:
            stored = self._find(message)
            if stored is not None:
                self._messages.remove(stored)
                stored.dlq_reason = reason
                self._dlq.append(stored)

    def list_dead_letters(self, limit: int = 10) -> list[Envelope]:
        with self._lock:
            return [m.envelope for m in self._dlq[:limit]]

    def check(self) -> None:
        return None

    # Test helpers
    @property
    def depth(self) -> int:
        return len(self._messages)

    @property
    def dlq_depth(self) -> int:
        return len(self._dlq)

    def dlq_reasons(self) -> list[str | None]:
        return [m.dlq_reason for m in self._dlq]


# --------------------------------------------------------------------------- SQS


class SqsQueue:
    """AWS SQS adapter (also works against ElasticMQ for local development)."""

    MAX_VISIBILITY_SECONDS = 43_200

    def __init__(
        self,
        *,
        queue_name: str,
        dlq_name: str,
        region: str,
        endpoint_url: str | None = None,
        visibility_timeout: int = 60,
        max_receive_count: int = 5,
        client: Any = None,
    ) -> None:
        if client is None:
            import boto3  # noqa: PLC0415 - keep boto3 import off the test fast-path

            client = boto3.client("sqs", region_name=region, endpoint_url=endpoint_url)
        self._client = client
        self._queue_name = queue_name
        self._dlq_name = dlq_name
        self._visibility_timeout = visibility_timeout
        self._max_receive_count = max_receive_count
        self._queue_url: str | None = None
        self._dlq_url: str | None = None

    def ensure_queues(self) -> None:
        """Create the work queue and DLQ with a redrive policy (idempotent).

        In AWS the queues are normally provisioned by infrastructure tooling;
        this is used for local ElasticMQ and first-time setup scripts.
        """
        dlq_url = self._client.create_queue(QueueName=self._dlq_name)["QueueUrl"]
        dlq_arn = self._client.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        redrive = json.dumps(
            {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(self._max_receive_count)}
        )
        url = self._client.create_queue(
            QueueName=self._queue_name,
            Attributes={
                "VisibilityTimeout": str(self._visibility_timeout),
                "RedrivePolicy": redrive,
            },
        )["QueueUrl"]
        self._queue_url, self._dlq_url = url, dlq_url

    @property
    def queue_url(self) -> str:
        if self._queue_url is None:
            self._queue_url = self._client.get_queue_url(QueueName=self._queue_name)["QueueUrl"]
        return self._queue_url

    @property
    def dlq_url(self) -> str:
        if self._dlq_url is None:
            self._dlq_url = self._client.get_queue_url(QueueName=self._dlq_name)["QueueUrl"]
        return self._dlq_url

    def send(self, envelope: Envelope, delay_seconds: int = 0) -> None:
        self._send(self.queue_url, envelope, delay_seconds)

    def _send(
        self, url: str, envelope: Envelope, delay_seconds: int = 0, reason: str | None = None
    ) -> None:
        attrs: dict[str, Any] = {"type": {"DataType": "String", "StringValue": envelope.type}}
        if envelope.correlation_id:
            attrs["correlation_id"] = {
                "DataType": "String",
                "StringValue": envelope.correlation_id,
            }
        if reason:
            attrs["dlq_reason"] = {"DataType": "String", "StringValue": reason[:256]}
        self._client.send_message(
            QueueUrl=url,
            MessageBody=envelope.model_dump_json(),
            DelaySeconds=min(max(delay_seconds, 0), 900),
            MessageAttributes=attrs,
        )

    def receive(self, max_messages: int = 10, wait_seconds: int = 0) -> list[ReceivedMessage]:
        resp = self._client.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=min(max_messages, 10),
            WaitTimeSeconds=wait_seconds,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        out: list[ReceivedMessage] = []
        for raw in resp.get("Messages", []):
            count = int(raw.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
            try:
                envelope = Envelope.model_validate_json(raw["Body"])
            except ValueError:
                # Poison message: cannot be parsed, so move it aside without processing.
                poison = Envelope(
                    message_id=raw.get("MessageId", "unknown"),
                    type="invalid",
                    payload={"raw": raw["Body"][:4096]},
                )
                self._send(self.dlq_url, poison, reason="unparseable_message")
                self._client.delete_message(
                    QueueUrl=self.queue_url, ReceiptHandle=raw["ReceiptHandle"]
                )
                continue
            out.append(ReceivedMessage(envelope, count, raw["ReceiptHandle"]))
        return out

    def ack(self, message: ReceivedMessage) -> None:
        self._client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message.receipt)

    def retry_later(self, message: ReceivedMessage, delay_seconds: int) -> None:
        self._client.change_message_visibility(
            QueueUrl=self.queue_url,
            ReceiptHandle=message.receipt,
            VisibilityTimeout=min(max(delay_seconds, 0), self.MAX_VISIBILITY_SECONDS),
        )

    def dead_letter(self, message: ReceivedMessage, reason: str) -> None:
        self._send(self.dlq_url, message.envelope, reason=reason)
        self.ack(message)

    def list_dead_letters(self, limit: int = 10) -> list[Envelope]:
        """Peek at DLQ messages (they become visible again after a short timeout)."""
        resp = self._client.receive_message(
            QueueUrl=self.dlq_url,
            MaxNumberOfMessages=min(limit, 10),
            VisibilityTimeout=1,
            WaitTimeSeconds=0,
        )
        return [Envelope.model_validate_json(m["Body"]) for m in resp.get("Messages", [])]

    def check(self) -> None:
        self._client.get_queue_attributes(
            QueueUrl=self.queue_url, AttributeNames=["ApproximateNumberOfMessages"]
        )


def build_queue(settings: Any) -> QueuePort:
    if settings.queue_backend == "memory":
        return InMemoryQueue(
            max_receive_count=settings.max_receive_count,
            visibility_timeout=settings.sqs_visibility_timeout_seconds,
        )
    return SqsQueue(
        queue_name=settings.sqs_queue_name,
        dlq_name=settings.sqs_dlq_name,
        region=settings.aws_region,
        endpoint_url=settings.sqs_endpoint_url,
        visibility_timeout=settings.sqs_visibility_timeout_seconds,
        max_receive_count=settings.max_receive_count,
    )
