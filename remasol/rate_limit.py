from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    """A small per-process limiter for the single-server MVP.

    It is intentionally not a distributed abuse system. The database state machine
    remains the payout safety boundary; this limiter only slows repeated requests.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        if not self.enabled:
            return True
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True
