"""In-process token-bucket rate limiter.

Limits are per API process; with N replicas the effective limit is N times
higher. A shared store (e.g. Redis) would be required for global limits.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class TokenBucketLimiter:
    per_minute: int
    clock: Callable[[], float] = time.monotonic
    max_keys: int = 10_000
    _buckets: dict[str, tuple[float, float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        capacity = float(self.per_minute)
        rate = capacity / 60.0
        now = self.clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (capacity, now))
            tokens = min(capacity, tokens + (now - last) * rate)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            if len(self._buckets) >= self.max_keys and key not in self._buckets:
                self._buckets.clear()  # bounded memory; resets are acceptable for a demo
            self._buckets[key] = (tokens, now)
            return allowed
