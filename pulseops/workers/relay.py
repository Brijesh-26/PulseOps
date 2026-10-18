"""Outbox relay: publishes committed outbox rows to the queue.

Rows are claimed with ``FOR UPDATE SKIP LOCKED`` so several relays can run
concurrently. A crash after ``send`` but before commit republishes the row,
which is safe because consumers are idempotent (at-least-once delivery).
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pulseops.config import Settings
from pulseops.messaging.models import OutboxMessage
from pulseops.messaging.queue import Envelope, QueuePort
from pulseops.observability.logging import get_logger
from pulseops.observability.metrics import OUTBOX_PUBLISH_FAILURES, OUTBOX_PUBLISHED
from pulseops.workers.backoff import backoff_delay

log = get_logger("pulseops.relay")


class OutboxRelay:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        queue: QueuePort,
        settings: Settings,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sessions = sessions
        self.queue = queue
        self.settings = settings
        self.now = now

    def run_once(self) -> int:
        published = 0
        with self.sessions() as session, session.begin():
            now = self.now()
            rows = session.scalars(
                select(OutboxMessage)
                .where(OutboxMessage.published_at.is_(None), OutboxMessage.available_at <= now)
                .order_by(OutboxMessage.created_at)
                .limit(self.settings.outbox_batch_size)
                .with_for_update(skip_locked=True)
            ).all()
            for row in rows:
                envelope = Envelope(
                    message_id=str(row.id),
                    type=row.message_type,
                    payload=row.payload,
                    correlation_id=row.correlation_id,
                    created_at=row.created_at,
                )
                try:
                    self.queue.send(envelope)
                except Exception as exc:
                    row.attempts += 1
                    row.last_error = f"{type(exc).__name__}: {exc}"[:1000]
                    row.available_at = now + timedelta(
                        seconds=backoff_delay(
                            row.attempts,
                            self.settings.retry_base_delay_seconds,
                            self.settings.retry_max_delay_seconds,
                        )
                    )
                    OUTBOX_PUBLISH_FAILURES.inc()
                    log.warning(
                        "outbox_publish_failed", outbox_id=str(row.id), error=type(exc).__name__
                    )
                    continue
                row.published_at = now
                row.attempts += 1
                published += 1
                OUTBOX_PUBLISHED.inc()
        return published

    def run_forever(self, stop: threading.Event) -> None:
        log.info("relay_started")
        while not stop.is_set():
            try:
                count = self.run_once()
            except Exception:
                log.exception("relay_iteration_failed")
                count = 0
            if count == 0:
                stop.wait(self.settings.outbox_poll_interval_seconds)
        log.info("relay_stopped")


def main() -> None:
    from pulseops.config import get_settings  # noqa: PLC0415
    from pulseops.observability.logging import configure_logging  # noqa: PLC0415
    from pulseops.runtime import Runtime  # noqa: PLC0415

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    runtime = Runtime.from_settings(settings)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    OutboxRelay(runtime.sessions, runtime.queue, settings).run_forever(stop)


if __name__ == "__main__":
    main()
