"""In-process rate limiter for password-verify attempts.

Brute-force protection: max N failed attempts per sliding window per IP.
A successful attempt does NOT reset the counter — that would allow
a correct guess to reset and try a new password batch.

Thread-safe (uses a lock).  Stored in process memory only — resets on
service restart.  This is acceptable: the service has Restart=always with a
2s RestartSec, so restarts already impose a delay; and the rate limit still
covers the common case of automated guessing in a single session.
"""

from __future__ import annotations

import threading
import time


_MAX_FAILURES = 5
_WINDOW_SECONDS = 60.0


class PasswordRateLimiter:
    """Track failed password attempts per client key (e.g. IP or process UID).

    Usage::

        limiter = PasswordRateLimiter()
        if limiter.is_blocked(key):
            return 429
        ok = verify_password(...)
        if not ok:
            limiter.record_failure(key)
    """

    def __init__(
        self,
        max_failures: int = _MAX_FAILURES,
        window_seconds: float = _WINDOW_SECONDS,
    ) -> None:
        self._max = max_failures
        self._window = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def is_blocked(self, key: str) -> bool:
        """Return True if *key* has too many recent failures."""
        with self._lock:
            return self._count_recent(key) >= self._max

    def record_failure(self, key: str) -> None:
        """Record one failed attempt for *key*."""
        now = time.monotonic()
        with self._lock:
            bucket = self._failures.setdefault(key, [])
            bucket.append(now)
            # Evict old entries to bound memory growth.
            cutoff = now - self._window
            self._failures[key] = [t for t in bucket if t > cutoff]

    def _count_recent(self, key: str) -> int:
        """Count failures within the window.  Must be called under the lock."""
        now = time.monotonic()
        cutoff = now - self._window
        return sum(1 for t in self._failures.get(key, []) if t > cutoff)
