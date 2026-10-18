"""Demo authentication: hashed API keys and short-lived HMAC-signed session tokens.

This is intentionally simple and is NOT production identity infrastructure:
no SSO/OIDC, no MFA, no key rotation workflow, in-process rate limits only.
See docs/security.md.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from pulseops.auth.models import ApiPrincipal
from pulseops.auth.principal import Principal, Role
from pulseops.errors import AuthenticationError, ConflictError

KEY_PREFIX_LEN = 12


def hash_key(raw_key: str) -> str:
    # API keys are 256-bit random values, so a fast hash is sufficient (no
    # password-stretching needed); lookup is by exact hash match.
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_api_key(kind: str = "live") -> str:
    return f"po_{kind}_{secrets.token_urlsafe(32)}"


def create_principal(
    session: Session, name: str, roles: list[Role], raw_key: str | None = None
) -> tuple[ApiPrincipal, str]:
    """Create a principal and return it together with the raw key (shown once)."""
    if session.scalar(select(ApiPrincipal).where(ApiPrincipal.name == name)):
        raise ConflictError(f"Principal {name!r} already exists", code="principal_exists")
    raw_key = raw_key or generate_api_key()
    if len(raw_key) < 24:
        raise ValueError("API keys must be at least 24 characters")
    principal = ApiPrincipal(
        name=name,
        key_prefix=raw_key[:KEY_PREFIX_LEN],
        key_hash=hash_key(raw_key),
        roles=[r.value for r in roles],
        is_active=True,
    )
    session.add(principal)
    session.flush()
    return principal, raw_key


def _to_principal(row: ApiPrincipal) -> Principal:
    return Principal(name=row.name, roles=frozenset(Role(r) for r in row.roles))


def authenticate_api_key(session: Session, raw_key: str) -> Principal:
    row = session.scalar(select(ApiPrincipal).where(ApiPrincipal.key_hash == hash_key(raw_key)))
    if row is None or not row.is_active:
        raise AuthenticationError("Invalid API key")
    row.last_used_at = datetime.now(UTC)
    return _to_principal(row)


def load_active_principal(session: Session, name: str) -> Principal:
    row = session.scalar(select(ApiPrincipal).where(ApiPrincipal.name == name))
    if row is None or not row.is_active:
        raise AuthenticationError("Principal is not active")
    return _to_principal(row)


# --------------------------------------------------------------------------- sessions


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


@dataclass(frozen=True)
class SessionToken:
    token: str
    expires_at: int


def issue_session_token(principal: Principal, secret: bytes, ttl_seconds: int) -> SessionToken:
    now = int(time.time())
    expires_at = now + ttl_seconds
    payload: dict[str, object] = {
        "sub": principal.name,
        "roles": sorted(r.value for r in principal.roles),
        "iat": now,
        "exp": expires_at,
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return SessionToken(token=f"{body}.{sig}", expires_at=expires_at)


def verify_session_token(token: str, secret: bytes, now: int | None = None) -> str:
    """Return the subject of a valid token; raise AuthenticationError otherwise."""
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise AuthenticationError("Invalid session token")
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthenticationError("Malformed session token") from exc
    if int(payload.get("exp", 0)) < (now if now is not None else int(time.time())):
        raise AuthenticationError("Session token expired")
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise AuthenticationError("Malformed session token")
    return sub
