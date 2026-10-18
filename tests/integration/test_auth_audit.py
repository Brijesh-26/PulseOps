from __future__ import annotations

import pytest
from sqlalchemy import create_engine, make_url, select, text, update
from sqlalchemy.exc import DBAPIError

from pulseops.audit import service as audit
from pulseops.audit.models import ActorType, AuditRecord
from pulseops.auth.models import ApiPrincipal
from pulseops.auth.principal import Role
from tests.conftest import Ctx


def test_session_token_flow(ctx: Ctx) -> None:
    resp = ctx.client.post("/api/v1/auth/session", json={"api_key": ctx.keys[Role.APPROVER]})
    assert resp.status_code == 200
    token = resp.json()["token"]
    me = ctx.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["principal"] == "test-approver"
    assert "proposals:decide" in me.json()["permissions"]
    assert "proposals:create" not in me.json()["permissions"]


def test_tampered_token_rejected(ctx: Ctx) -> None:
    token = ctx.client.post("/api/v1/auth/session", json={"api_key": ctx.keys[Role.VIEWER]}).json()[
        "token"
    ]
    body, sig = token.split(".")
    forged = body[:-2] + ("AA" if body[-2:] != "AA" else "BB") + "." + sig
    resp = ctx.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


def test_deactivated_principal_loses_access_immediately(ctx: Ctx) -> None:
    token = ctx.client.post("/api/v1/auth/session", json={"api_key": ctx.keys[Role.VIEWER]}).json()[
        "token"
    ]
    with ctx.session() as s:
        s.execute(
            update(ApiPrincipal).where(ApiPrincipal.name == "test-viewer").values(is_active=False)
        )
        s.commit()
    resp = ctx.client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_failed_login_is_audited(ctx: Ctx) -> None:
    resp = ctx.client.post("/api/v1/auth/session", json={"api_key": "po_live_" + "z" * 40})
    assert resp.status_code == 401
    with ctx.session() as s:
        rec = s.scalars(select(AuditRecord).where(AuditRecord.action == "auth.login_failed")).one()
        assert rec.details == {"key_prefix": "po_live_"}


def test_audit_chain_verifies_and_detects_tampering(ctx: Ctx, engine) -> None:  # type: ignore[no-untyped-def]
    with ctx.session() as s:
        for i in range(5):
            audit.record(
                s,
                actor="tester",
                actor_type=ActorType.USER,
                action="test.action",
                entity_type="thing",
                entity_id=str(i),
                details={"i": i, "api_key": "should-be-redacted"},
            )
        s.commit()
        assert audit.verify_chain(s).valid
        first = s.scalars(select(AuditRecord).order_by(AuditRecord.id)).first()
        assert first is not None and first.details["api_key"] == "[REDACTED]"

    # Simulate an attacker with superuser access bypassing the triggers.
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("UPDATE audit_record SET actor = 'mallory' WHERE id = 3"))
    with ctx.session() as s:
        result = audit.verify_chain(s)
        assert not result.valid
        assert result.first_invalid_id == 3


def test_audit_records_cannot_be_modified_by_owner(ctx: Ctx) -> None:
    with ctx.session() as s:
        audit.record(
            s, actor="a", actor_type=ActorType.SYSTEM, action="x", entity_type="y", entity_id="z"
        )
        s.commit()
        with pytest.raises(DBAPIError, match="append-only"):
            s.execute(text("DELETE FROM audit_record"))
        s.rollback()
        with pytest.raises(DBAPIError, match="append-only"):
            s.execute(text("TRUNCATE audit_record"))
        s.rollback()


def test_app_role_lacks_update_privilege_on_audit(ctx: Ctx, database_url: str) -> None:
    url = make_url(database_url).set(username="pulseops_app", password="pulseops_app")
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT count(*) FROM audit_record"))
            with pytest.raises(DBAPIError, match="permission denied"):
                conn.execute(text("UPDATE audit_record SET actor = 'x'"))
    except DBAPIError as exc:
        if "password authentication failed" in str(exc) or "does not exist" in str(exc):
            pytest.skip("pulseops_app role not provisioned in this environment")
        raise
    finally:
        eng.dispose()
