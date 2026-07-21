"""Simple in-process rate limiter for login attempts."""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class LoginRateLimiter:
    def __init__(self, *, max_failures: int = 8, window_seconds: int = 300) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = defaultdict(list)

    def _prune(self, key: str, now: float) -> None:
        cutoff = now - self.window_seconds
        self._failures[key] = [t for t in self._failures[key] if t >= cutoff]

    def is_blocked(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            self._prune(key, now)
            return len(self._failures[key]) >= self.max_failures

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            self._prune(key, now)
            self._failures[key].append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def remaining_seconds(self, key: str) -> int:
        now = time.time()
        with self._lock:
            self._prune(key, now)
            if len(self._failures[key]) < self.max_failures:
                return 0
            oldest = min(self._failures[key])
            return max(0, int(self.window_seconds - (now - oldest)) + 1)


login_limiter = LoginRateLimiter()
