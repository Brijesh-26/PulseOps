from __future__ import annotations

import random


def backoff_delay(
    attempt: int, base: float, maximum: float, rng: random.Random | None = None
) -> int:
    """Exponential backoff with "equal jitter": half fixed, half random.

    attempt=1 -> ~base, attempt=2 -> ~2*base, … capped at ``maximum`` seconds.
    """
    exp = min(maximum, base * (2 ** max(attempt - 1, 0)))
    r = (rng or random).random()
    return max(1, int(exp / 2 + r * exp / 2))
