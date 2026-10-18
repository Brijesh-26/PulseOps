from __future__ import annotations

import uuid

from pulseops.messaging.queue import Envelope
from pulseops.remediation.service import execute_proposal, mark_execution_failed
from pulseops.runtime import Runtime
from pulseops.workers.worker import Handler


def build_execution_handler(runtime: Runtime) -> Handler:
    from pulseops.workers.handlers import payload_uuid  # noqa: PLC0415

    def handle(env: Envelope) -> None:
        with runtime.sessions() as session, session.begin():
            execute_proposal(session, payload_uuid(env, "proposal_id"), runtime.sandbox)

    def on_failure(env: Envelope, error: str, final: bool) -> None:
        if not final:
            return
        try:
            proposal_id = uuid.UUID(str(env.payload.get("proposal_id")))
        except ValueError:
            return
        with runtime.sessions() as session, session.begin():
            mark_execution_failed(session, proposal_id, error)

    return Handler(handle, on_failure)
