import itertools

import pytest

from pulseops.errors import InvalidTransitionError
from pulseops.incidents.models import IncidentStatus as S
from pulseops.incidents.state import (
    MANUAL_TRANSITIONS,
    TRANSITIONS,
    assert_transition,
    can_transition,
)


def test_every_status_has_a_transition_entry() -> None:
    assert set(TRANSITIONS) == set(S)


def test_happy_path_is_valid() -> None:
    path = [S.OPEN, S.INVESTIGATING, S.ACTION_REQUIRED, S.RECOVERING, S.RESOLVED, S.CLOSED]
    for a, b in itertools.pairwise(path):
        assert_transition(a, b)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.OPEN, S.RECOVERING),  # cannot recover without an approved proposal
        (S.INVESTIGATING, S.RESOLVED),  # AI cannot resolve an incident
        (S.INVESTIGATING, S.RECOVERING),
        (S.CLOSED, S.OPEN),  # closed is terminal
        (S.RESOLVED, S.RECOVERING),
        (S.OPEN, S.OPEN),
    ],
)
def test_invalid_transitions_rejected(current: S, target: S) -> None:
    assert not can_transition(current, target)
    with pytest.raises(InvalidTransitionError) as exc:
        assert_transition(current, target)
    assert exc.value.details == {"from": current.value, "to": target.value}


def test_closed_is_terminal() -> None:
    assert TRANSITIONS[S.CLOSED] == frozenset()


def test_manual_transitions_are_subset_of_valid_ones() -> None:
    for a, b in MANUAL_TRANSITIONS:
        assert can_transition(a, b)


def test_workflow_transitions_cannot_be_forced_manually() -> None:
    with pytest.raises(InvalidTransitionError):
        assert_transition(S.ACTION_REQUIRED, S.RECOVERING, manual=True)
    assert_transition(S.OPEN, S.CLOSED, manual=True)
