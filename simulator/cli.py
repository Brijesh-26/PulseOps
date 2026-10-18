"""Scenario CLI.

    python -m simulator list
    python -m simulator run a_db_connection_failure      # or just: run a

``run`` seeds the scenario's orchestrator state into the database configured
by PULSEOPS_DATABASE_URL and then POSTs its events to the ingestion API
(PULSEOPS_API_URL, default http://localhost:8000) using the ingest key in
PULSEOPS_INGEST_KEY, exactly as an external producer would.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys

import httpx


def main(argv: list[str] | None = None) -> int:
    from simulator.loader import get_scenario, list_scenarios, seed_scenario  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="simulator")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    ev = sub.add_parser("evaluate", help="run all scenarios on a fresh database and write a report")
    ev.add_argument(
        "--admin-database-url",
        default=os.environ.get(
            "PULSEOPS_EVAL_DATABASE_URL",
            "postgresql+psycopg://pulseops:pulseops@localhost:5433/pulseops_eval",
        ),
        help="owner URL; the named database is DROPPED and recreated",
    )
    ev.add_argument("--provider", choices=["mock", "anthropic"], default="mock")
    ev.add_argument("--out", default="reports")
    run = sub.add_parser("run")
    run.add_argument("scenario")
    run.add_argument("--api", default=os.environ.get("PULSEOPS_API_URL", "http://localhost:8000"))
    run.add_argument("--tag", default=None, help="suffix for event/run ids (default: random)")
    args = parser.parse_args(argv)

    if args.cmd == "list":
        for sc in list_scenarios():
            print(f"{sc.key:34} {sc.title}")
        return 0

    if args.cmd == "evaluate":
        from simulator.report import evaluate  # noqa: PLC0415

        return evaluate(args.admin_database_url, args.provider, args.out)

    key = os.environ.get("PULSEOPS_INGEST_KEY")
    if not key:
        print("PULSEOPS_INGEST_KEY is required (an API key with the ingest role)", file=sys.stderr)
        return 2
    scenario = get_scenario(args.scenario)
    tag = args.tag or secrets.token_hex(3)

    from pulseops.db.session import session_scope  # noqa: PLC0415

    with session_scope() as s:
        payloads = seed_scenario(s, scenario, tag=tag)
    print(f"seeded {scenario.key} (tag={tag}); delivering {len(payloads)} event(s)")
    with httpx.Client(base_url=args.api, timeout=10, headers={"X-Api-Key": key}) as client:
        for p in payloads:
            r = client.post("/api/v1/events", json=p)
            body = r.json()
            print(f"  {p['event_id']:40} -> {r.status_code} duplicate={body.get('duplicate')}")
            if r.status_code >= 400:
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
