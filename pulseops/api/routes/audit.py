from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from pulseops.api.deps import DbSession, require
from pulseops.api.pagination import Page, PageParams
from pulseops.api.schemas import AuditOut
from pulseops.audit.models import AuditRecord
from pulseops.audit.service import verify_chain
from pulseops.auth.principal import Permission, Principal

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])
Auditor = Annotated[Principal, Depends(require(Permission.AUDIT_READ))]


class ChainStatus(BaseModel):
    valid: bool
    records_checked: int
    first_invalid_id: int | None


@router.get("", response_model=Page[AuditOut], summary="Query the append-only audit trail")
def list_audit(
    session: DbSession,
    _: Auditor,
    page: Annotated[PageParams, Depends()],
    incident_id: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    actor: Annotated[str | None, Query(max_length=100)] = None,
) -> Page[AuditOut]:
    stmt = select(AuditRecord)
    if incident_id:
        stmt = stmt.where(AuditRecord.incident_id == incident_id)
    if action:
        stmt = stmt.where(AuditRecord.action == action)
    if actor:
        stmt = stmt.where(AuditRecord.actor == actor)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.order_by(AuditRecord.id.desc()).limit(page.limit).offset(page.offset)
    ).all()
    return Page[AuditOut](
        items=[AuditOut.model_validate(r) for r in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/verify", response_model=ChainStatus, summary="Verify the audit hash chain")
def verify(session: DbSession, _: Auditor) -> ChainStatus:
    result = verify_chain(session)
    return ChainStatus(
        valid=result.valid,
        records_checked=result.records_checked,
        first_invalid_id=result.first_invalid_id,
    )
