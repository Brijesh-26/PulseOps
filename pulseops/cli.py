"""Operational CLI: ``python -m pulseops.cli <command>``."""

from __future__ import annotations

import argparse
import os
import sys

from pulseops.auth.principal import Role
from pulseops.config import get_settings

DEMO_PRINCIPALS = {
    "demo-ingest": [Role.INGEST],
    "demo-viewer": [Role.VIEWER],
    "demo-operator": [Role.OPERATOR],
    "demo-approver": [Role.APPROVER],
    "demo-approver-2": [Role.APPROVER],
    "demo-admin": [Role.ADMIN],
}


def _out(msg: str) -> None:
    sys.stdout.write(msg + "\n")


def migrate() -> None:
    from alembic import command  # noqa: PLC0415
    from alembic.config import Config  # noqa: PLC0415

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(root, "migrations"))
    command.upgrade(cfg, "head")
    _out("migrations: up to date")


def ensure_queues() -> None:
    from pulseops.messaging.queue import SqsQueue, build_queue  # noqa: PLC0415

    queue = build_queue(get_settings())
    if isinstance(queue, SqsQueue):
        queue.ensure_queues()
        _out("queues: work queue and DLQ ensured (redrive policy set)")


def seed_demo_principals() -> None:
    from sqlalchemy import select  # noqa: PLC0415

    from pulseops.auth.models import ApiPrincipal  # noqa: PLC0415
    from pulseops.auth.service import create_principal  # noqa: PLC0415
    from pulseops.db.session import session_scope  # noqa: PLC0415

    with session_scope() as s:
        for name, roles in DEMO_PRINCIPALS.items():
            if s.scalar(select(ApiPrincipal).where(ApiPrincipal.name == name)):
                continue
            env = "PULSEOPS_DEMO_KEY_" + name.removeprefix("demo-").replace("-", "_").upper()
            raw = os.environ.get(env)
            _, key = create_principal(s, name, roles, raw_key=raw)
            shown = "(from $" + env + ")" if raw else key
            _out(f"created principal {name} roles={[r.value for r in roles]} key={shown}")


def bootstrap() -> None:
    settings = get_settings()
    migrate()
    ensure_queues()
    if settings.demo_mode:
        seed_demo_principals()


def create_key(name: str, roles: list[str]) -> None:
    from pulseops.auth.service import create_principal  # noqa: PLC0415
    from pulseops.db.session import session_scope  # noqa: PLC0415

    with session_scope() as s:
        _, key = create_principal(s, name, [Role(r) for r in roles])
    _out(f"principal {name} created. API key (shown once): {key}")


def verify_audit() -> int:
    from pulseops.audit.service import verify_chain  # noqa: PLC0415
    from pulseops.db.session import session_scope  # noqa: PLC0415

    with session_scope() as s:
        result = verify_chain(s)
    _out(f"audit chain valid={result.valid} checked={result.records_checked}")
    return 0 if result.valid else 1


def dead_letters(limit: int) -> None:
    from pulseops.messaging.queue import build_queue  # noqa: PLC0415

    for env in build_queue(get_settings()).list_dead_letters(limit):
        _out(f"{env.message_id} type={env.type} payload={env.payload}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pulseops")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("bootstrap", help="migrate, ensure queues, seed demo principals")
    key = sub.add_parser("create-key")
    key.add_argument("name")
    key.add_argument("--role", action="append", required=True, choices=[r.value for r in Role])
    sub.add_parser("verify-audit")
    dlq = sub.add_parser("dead-letters")
    dlq.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)

    if args.cmd == "migrate":
        migrate()
    elif args.cmd == "bootstrap":
        bootstrap()
    elif args.cmd == "create-key":
        create_key(args.name, args.role)
    elif args.cmd == "verify-audit":
        return verify_audit()
    elif args.cmd == "dead-letters":
        dead_letters(args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
