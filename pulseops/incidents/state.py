"""Incident lifecycle state machine. The single source of truth for valid transitions."""

from __future__ import annotations

from pulseops.errors import InvalidTransitionError
from pulseops.incidents.models import IncidentStatus as S

TRANSITIONS: dict[S, frozenset[S]] = {
    S.OPEN: frozenset({S.INVESTIGATING, S.ACTION_REQUIRED, S.RESOLVED, S.CLOSED}),
    S.INVESTIGATING: frozenset({S.OPEN, S.ACTION_REQUIRED}),
    S.ACTION_REQUIRED: frozenset({S.RECOVERING, S.OPEN, S.INVESTIGATING}),
    S.RECOVERING: frozenset({S.RESOLVED, S.OPEN}),
    S.RESOLVED: frozenset({S.CLOSED, S.OPEN}),
    S.CLOSED: frozenset(),
}

# Transitions a human may request directly via the API. Everything else is
# driven by the workflow (investigation, approval, execution).
MANUAL_TRANSITIONS: frozenset[tuple[S, S]] = frozenset(
    {
        (S.OPEN, S.RESOLVED),
        (S.OPEN, S.CLOSED),
        (S.RESOLVED, S.CLOSED),
        (S.RESOLVED, S.OPEN),
    }
)


def can_transition(current: S, target: S) -> bool:
    return target in TRANSITIONS[current]


def assert_transition(current: S, target: S, *, manual: bool = False) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError(
            f"Cannot move incident from {current} to {target}",
            details={"from": current.value, "to": target.value},
        )
    if manual and (current, target) not in MANUAL_TRANSITIONS:
        raise InvalidTransitionError(
            f"Transition {current} -> {target} is workflow-controlled and cannot be set manually",
            details={"from": current.value, "to": target.value},
        )
