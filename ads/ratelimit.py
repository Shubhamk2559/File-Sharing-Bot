"""Tiny in-memory sliding-window limiter. Per process (documented limitation for multi-instance)."""
from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    def __init__(self, max_events: int, window: float, clock=time.monotonic):
        self.max, self.window, self.clock = max_events, window, clock
        self._hits: dict = {}

    def allow(self, key) -> bool:
        now = self.clock()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max:
            return False
        q.append(now)
        if len(self._hits) > 20000:           # crude memory cap
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window][:5000]:
                self._hits.pop(k, None)
        return True
