"""Shared fixtures. Integration tests run against a real PostgreSQL database.

Set PULSEOPS_TEST_DATABASE_URL to an owner/superuser URL; a dedicated
``*_test`` database is created and migrated once per session. Locally the
default points at the docker-compose Postgres (localhost:5433).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.orm import Session

from pulseops.api.app import create_app
from pulseops.auth.principal import Role
from pulseops.auth.service import create_principal
from pulseops.config import Settings
from pulseops.db.models import Base
from pulseops.messaging.queue import InMemoryQueue
from pulseops.runtime import Runtime

DEFAULT_TEST_URL = "postgresql+psycopg://pulseops:pulseops@localhost:5433/pulseops_test"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _test_url() -> str:
    return os.environ.get("PULSEOPS_TEST_DATABASE_URL", DEFAULT_TEST_URL)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = make_url(_test_url())
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    except Exception as exc:
        if "PULSEOPS_TEST_DATABASE_URL" in os.environ:
            raise
        pytest.skip(f"PostgreSQL not available for integration tests: {type(exc).__name__}")
    finally:
        admin.dispose()

    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.attributes["database_url"] = url.render_as_string(hide_password=False)
    command.upgrade(cfg, "head")
    yield url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    eng = create_engine(database_url)
    yield eng
    eng.dispose()


def truncate_all(engine: Engine) -> None:
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        # Superuser-only: disables the append-only audit triggers for test cleanup.
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@dataclass
class Ctx:
    runtime: Runtime
    client: TestClient
    queue: InMemoryQueue
    keys: dict[Role, str]
    second_approver_key: str

    def session(self) -> Session:
        return self.runtime.sessions()

    def headers(self, role: Role) -> dict[str, str]:
        return {"X-Api-Key": self.keys[role]}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def test_settings(database_url: str) -> Settings:
    return Settings(
        environment="test",
        database_url=database_url,
        queue_backend="memory",
        log_json=False,
        log_level="WARNING",
        demo_mode=True,
        max_receive_count=3,
        retry_base_delay_seconds=1,
        retry_max_delay_seconds=10,
        rate_limit_per_minute=0,
        auth_rate_limit_per_minute=0,
    )


@pytest.fixture
def ctx(engine: Engine, test_settings: Settings, clock: FakeClock) -> Iterator[Ctx]:
    truncate_all(engine)
    queue = InMemoryQueue(max_receive_count=3, visibility_timeout=30, clock=clock)
    runtime = Runtime.from_settings(test_settings, queue=queue)
    keys: dict[Role, str] = {}
    with runtime.sessions() as s:
        for role in Role:
            _, raw = create_principal(s, f"test-{role.value}", [role])
            keys[role] = raw
        _, second = create_principal(s, "test-approver-2", [Role.APPROVER])
        s.commit()
    client = TestClient(create_app(runtime))
    yield Ctx(runtime=runtime, client=client, queue=queue, keys=keys, second_approver_key=second)
    runtime.engine.dispose()
