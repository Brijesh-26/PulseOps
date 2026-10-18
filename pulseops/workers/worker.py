"""Queue consumer with bounded retries, backoff and dead-lettering.

Failure policy per message:
- handler succeeds                -> ack
- ``PermanentError``              -> dead-letter immediately
- any other exception, attempts left -> make visible again after backoff
- any other exception, attempts exhausted -> dead-letter

``on_failure`` hooks record the failure on the domain aggregate (e.g. event
status FAILED / DEAD_LETTERED) in a separate transaction, so the failure is
visible even though the handler's own transaction was rolled back.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass

from pulseops.config import Settings
from pulseops.errors import PermanentError
from pulseops.messaging.queue import Envelope, QueuePort, ReceivedMessage
from pulseops.observability.logging import get_logger, reset_correlation_id, set_correlation_id
from pulseops.observability.metrics import MESSAGES_DEAD_LETTERED, MESSAGES_PROCESSED
from pulseops.workers.backoff import backoff_delay

log = get_logger("pulseops.worker")


@dataclass(frozen=True)
class Handler:
    handle: Callable[[Envelope], None]
    # (envelope, error message, final) -> None
    on_failure: Callable[[Envelope, str, bool], None] | None = None


class Worker:
    def __init__(self, queue: QueuePort, handlers: dict[str, Handler], settings: Settings) -> None:
        self.queue = queue
        self.handlers = handlers
        self.settings = settings

    def run_once(self, max_messages: int = 10, wait_seconds: int = 0) -> int:
        messages = self.queue.receive(max_messages=max_messages, wait_seconds=wait_seconds)
        for message in messages:
            self.process(message)
        return len(messages)

    def drain(self, max_iterations: int = 100) -> int:
        """Process until the queue yields nothing (tests and local tooling)."""
        total = 0
        for _ in range(max_iterations):
            n = self.run_once()
            if n == 0:
                break
            total += n
        return total

    def process(self, message: ReceivedMessage) -> None:
        env = message.envelope
        token = set_correlation_id(env.correlation_id)
        try:
            handler = self.handlers.get(env.type)
            if handler is None:
                self._dead_letter(message, f"unknown message type {env.type!r}", None)
                return
            try:
                handler.handle(env)
            except PermanentError as exc:
                self._dead_letter(message, f"permanent: {exc}", handler)
                return
            except Exception as exc:
                self._retry_or_dead_letter(message, exc, handler)
                return
            self.queue.ack(message)
            MESSAGES_PROCESSED.labels(env.type, "ok").inc()
            log.info("message_processed", message_type=env.type, message_id=env.message_id)
        finally:
            reset_correlation_id(token)

    def _retry_or_dead_letter(
        self, message: ReceivedMessage, exc: Exception, handler: Handler
    ) -> None:
        env = message.envelope
        error = f"{type(exc).__name__}: {exc}"[:1000]
        if message.receive_count >= self.settings.max_receive_count:
            self._dead_letter(message, f"max attempts exhausted: {error}", handler)
            return
        delay = backoff_delay(
            message.receive_count,
            self.settings.retry_base_delay_seconds,
            self.settings.retry_max_delay_seconds,
        )
        self._notify(handler, env, error, final=False)
        self.queue.retry_later(message, delay)
        MESSAGES_PROCESSED.labels(env.type, "retry").inc()
        log.warning(
            "message_retry_scheduled",
            message_type=env.type,
            message_id=env.message_id,
            attempt=message.receive_count,
            delay_seconds=delay,
            error=error,
        )

    def _dead_letter(self, message: ReceivedMessage, reason: str, handler: Handler | None) -> None:
        env = message.envelope
        if handler is not None:
            self._notify(handler, env, reason, final=True)
        self.queue.dead_letter(message, reason)
        MESSAGES_DEAD_LETTERED.labels(env.type).inc()
        MESSAGES_PROCESSED.labels(env.type, "dead_lettered").inc()
        log.error(
            "message_dead_lettered",
            message_type=env.type,
            message_id=env.message_id,
            attempt=message.receive_count,
            reason=reason,
        )

    @staticmethod
    def _notify(handler: Handler, env: Envelope, error: str, final: bool) -> None:
        if handler.on_failure is None:
            return
        try:
            handler.on_failure(env, error, final)
        except Exception:
            log.exception("failure_hook_failed", message_type=env.type)

    def run_forever(self, stop: threading.Event) -> None:
        log.info("worker_started", handlers=sorted(self.handlers))
        while not stop.is_set():
            try:
                self.run_once(wait_seconds=10)
            except Exception:
                log.exception("worker_iteration_failed")
                stop.wait(2)
        log.info("worker_stopped")


def main() -> None:
    from pulseops.config import get_settings  # noqa: PLC0415
    from pulseops.observability.logging import configure_logging  # noqa: PLC0415
    from pulseops.runtime import Runtime  # noqa: PLC0415
    from pulseops.workers.handlers import build_handlers  # noqa: PLC0415

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    runtime = Runtime.from_settings(settings)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    Worker(runtime.queue, build_handlers(runtime), settings).run_forever(stop)


if __name__ == "__main__":
    main()
