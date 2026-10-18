"""Simulated environment state. Recovery actions can only mutate these tables
(plus simulated task executions and dataset staleness flags)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pulseops.db.base import Base, tz, utcnow


class SimResource(Base):
    """A simulated external dependency, e.g. a warehouse database or source API."""

    __tablename__ = "sim_resource"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    healthy: Mapped[bool] = mapped_column(default=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=5)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(tz(), default=utcnow, onupdate=utcnow)


class SimFixture(Base):
    """A test fixture batch that can be safely reprocessed inside the simulator."""

    __tablename__ = "sim_fixture"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))  # CORRUPT | VALID
    checksum: Mapped[str] = mapped_column(String(64))
    expected_checksum: Mapped[str] = mapped_column(String(64))
    reprocess_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(tz(), default=utcnow, onupdate=utcnow)
