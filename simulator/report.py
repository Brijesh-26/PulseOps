"""Evaluation report: run every scenario on a fresh database and record the results.

The report is generated from actual runs. Nothing in it is hand-written except
the explanatory text in the template.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text

from pulseops.config import Settings
from pulseops.messaging.queue import InMemoryQueue
from pulseops.runtime import Runtime
from simulator.evaluation import ScenarioResult, run_scenario, summarise
from simulator.loader import list_scenarios

ROOT = Path(__file__).resolve().parent.parent


def _fresh_database(url: str) -> str:
    u = make_url(url)
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{u.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{u.database}"'))
    admin.dispose()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = u.render_as_string(hide_password=False)
    command.upgrade(cfg, "head")
    return u.render_as_string(hide_password=False)


def _markdown(
    results: list[ScenarioResult], summary: dict[str, object], meta: dict[str, str]
) -> str:
    def yn(v: object) -> str:
        return "n/a" if v is None else ("yes" if v else "**no**")

    rows = [
        "| Scenario | Root cause (expected → actual) | Action (expected → actual) | Confidence "
        "| Cited | Invalid cites | Flagged claims | Impact ok | Pass |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        rows.append(
            f"| {r.scenario} | {r.root_cause_expected or '—'} → {r.root_cause_actual} "
            f"| {r.action_expected or 'none'} → {r.action_actual or 'none'} "
            f"| {r.confidence} | {r.cited_evidence} | {r.invalid_citations} "
            f"| {r.unsupported_claims_flagged} | {yn(r.impact_ok)} | {yn(r.passed)} |"
        )
    notes = [f"- {r.scenario}: {'; '.join(r.notes)}" for r in results if r.notes] or ["- none"]
    return "\n".join(
        [
            "# Evaluation results",
            "",
            f"Generated {meta['generated_at']} by `python -m simulator evaluate "
            f"--provider {meta['provider']}` (model: {meta['model']}).",
            "",
            "## Summary",
            "",
            "```json",
            json.dumps(summary, indent=2),
            "```",
            "",
            "## Per scenario",
            "",
            *rows,
            "",
            "## Notes",
            "",
            *notes,
            "",
        ]
    )


def evaluate(admin_url: str, provider: str, out: str) -> int:
    url = _fresh_database(admin_url)
    settings = Settings(
        environment="test",
        database_url=url,
        queue_backend="memory",
        investigator_provider=provider,
        anthropic_api_key=os.environ.get("PULSEOPS_ANTHROPIC_API_KEY") or None,
        log_json=False,
        log_level="WARNING",
    )
    runtime = Runtime.from_settings(settings, queue=InMemoryQueue())
    results = [run_scenario(runtime, s, tag="eval") for s in list_scenarios()]
    summary = summarise(results)
    meta = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "provider": provider,
        "model": settings.anthropic_model if provider == "anthropic" else "mock-rules-v1",
    }
    Path(out).mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "summary": summary, "results": [r.as_dict() for r in results]}
    Path(out, f"evaluation-{provider}.json").write_text(json.dumps(payload, indent=2, default=str))
    Path(out, f"evaluation-{provider}.md").write_text(_markdown(results, summary, meta))
    print(json.dumps(summary, indent=2))
    runtime.engine.dispose()
    return 0 if summary["passed"] == summary["scenarios"] else 1
