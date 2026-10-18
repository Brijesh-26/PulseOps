"""Transactional outbox: messages written in the same transaction as domain state."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pulseops.db.base import Base, created_at_col, tz, utcnow, uuid_pk


class OutboxMessage(Base):
    __tablename__ = "outbox_message"
    __table_args__ = (
        Index(
            "ix_outbox_message_pending",
            "available_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    message_type: Mapped[str] = mapped_column(String(64))
    aggregate_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_at_col()
    available_at: Mapped[datetime] = mapped_column(tz(), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(tz())
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
