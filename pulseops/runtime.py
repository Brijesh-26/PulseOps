"""Process-wide runtime wiring (composition root).

Everything that has an external dependency (database, queue, LLM provider)
is constructed here so tests can substitute deterministic implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from pulseops.config import Settings
from pulseops.db.session import make_engine
from pulseops.messaging.queue import QueuePort, build_queue

if TYPE_CHECKING:
    from pulseops.investigator.provider import InvestigationProvider
    from pulseops.remediation.port import ExecutionSandbox


@dataclass
class Runtime:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    queue: QueuePort
    _provider: InvestigationProvider | None = field(default=None)
    _sandbox: ExecutionSandbox | None = field(default=None)

    @classmethod
    def from_settings(cls, settings: Settings, queue: QueuePort | None = None) -> Runtime:
        engine = make_engine(settings.database_url)
        return cls(
            settings=settings,
            engine=engine,
            sessions=sessionmaker(bind=engine, expire_on_commit=False, autoflush=False),
            queue=queue or build_queue(settings),
        )

    @property
    def provider(self) -> InvestigationProvider:
        if self._provider is None:
            from pulseops.investigator.provider import build_provider  # noqa: PLC0415

            self._provider = build_provider(self.settings)
        return self._provider

    @provider.setter
    def provider(self, value: InvestigationProvider) -> None:
        self._provider = value

    @property
    def sandbox(self) -> ExecutionSandbox:
        if self._sandbox is None:
            from simulator.executor import SimulatorSandbox  # noqa: PLC0415

            self._sandbox = SimulatorSandbox()
        return self._sandbox
