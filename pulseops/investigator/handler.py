from __future__ import annotations

import uuid

from pulseops.investigator.service import fail_run_permanently, run_investigation
from pulseops.messaging.queue import Envelope
from pulseops.runtime import Runtime
from pulseops.workers.worker import Handler


def build_investigation_handler(runtime: Runtime) -> Handler:
    from pulseops.workers.handlers import payload_uuid  # noqa: PLC0415

    def handle(env: Envelope) -> None:
        run_investigation(runtime, payload_uuid(env, "run_id"))

    def on_failure(env: Envelope, error: str, final: bool) -> None:
        if not final:
            return
        try:
            run_id = uuid.UUID(str(env.payload.get("run_id")))
        except ValueError:
            return
        fail_run_permanently(runtime, run_id, error)

    return Handler(handle, on_failure)
