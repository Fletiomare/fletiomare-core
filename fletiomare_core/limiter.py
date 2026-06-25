"""In-memory brute-force throttle for the public /login edge."""
from __future__ import annotations

import threading
import time
from typing import Dict, List


class LoginLimiter:
    """In-memory, failures-only sliding-window throttle, keyed by one or more
    strings (e.g. client IP, username). A successful login resets the keys.

    Per-process state — adequate here: each instance enforcing its own share at
    the public edge still defeats brute force. Keys with no recent failures are
    dropped, so memory stays bounded."""

    def __init__(self, max_failures: int = 5, window: int = 300) -> None:
        self.max = max_failures
        self.window = window
        self._fails: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> List[float]:
        q = [t for t in self._fails.get(key, ()) if t > now - self.window]
        if q:
            self._fails[key] = q
        else:
            self._fails.pop(key, None)
        return q

    def retry_after(self, *keys: str) -> int:
        """0 if allowed; else whole seconds until the most-limited key frees up."""
        now = time.time()
        wait = 0
        with self._lock:
            for key in keys:
                if not key:
                    continue
                q = self._prune(key, now)
                if len(q) >= self.max:
                    wait = max(wait, int(self.window - (now - q[0])) + 1)
        return wait

    def record_failure(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for key in keys:
                if not key:
                    continue
                self._prune(key, now)
                self._fails.setdefault(key, []).append(now)

    def reset(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._fails.pop(key, None)
