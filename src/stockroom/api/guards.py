"""Spend and abuse guards for the public demo. Each one is small, in-memory and unit-tested.

The demo spends real API credit on every question, so the guards fail closed. Once today's budget is
spent, chat is off until midnight UTC and the UI shows the recorded eval results instead.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections import deque
from collections.abc import Callable


class DailyBudget:
    """Global $ cap per UTC day. `allow()` is checked before a turn; `add()` records its real cost.

    A turn that starts under the cap may finish over it. The overshoot is bounded by
    max_concurrent x per-conversation cap, which the API keeps small.
    """

    def __init__(self, limit_usd: float, today: Callable[[], dt.date] | None = None) -> None:
        self.limit_usd = limit_usd
        self._today = today or (lambda: dt.datetime.now(dt.UTC).date())
        self._day = self._today()
        self._spent = 0.0
        self._lock = threading.Lock()

    def _roll(self) -> None:
        if (d := self._today()) != self._day:
            self._day, self._spent = d, 0.0

    @property
    def spent_usd(self) -> float:
        with self._lock:
            self._roll()
            return self._spent

    def allow(self) -> bool:
        return self.spent_usd < self.limit_usd

    def add(self, usd: float) -> None:
        with self._lock:
            self._roll()
            self._spent += max(0.0, usd)


class RateLimiter:
    """At most `limit` events per `window_s` per key (sliding window)."""

    def __init__(
        self, limit: int, window_s: float = 3600.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit, self.window_s, self._clock = limit, window_s, clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> tuple[bool, float]:
        """Record an attempt. Returns (allowed, seconds until the next slot frees up)."""
        now = self._clock()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False, self.window_s - (now - q[0])
            q.append(now)
            if len(self._hits) > 10_000:  # forget idle keys so memory stays bounded
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True, 0.0
