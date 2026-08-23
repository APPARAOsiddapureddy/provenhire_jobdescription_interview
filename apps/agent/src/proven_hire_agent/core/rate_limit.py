"""Lightweight in-process rate limiting (Phase 5 of the live-interview
backend hardening plan). Genuinely new code — no rate-limiting precedent
existed anywhere in this codebase before this.

Sliding-window counter keyed by an arbitrary string (a session_id for the
live WS route, a client IP for the REST routes). Explicitly scoped to the
single-process assumption already recorded at startup (``app.py``'s
``_lifespan`` log line): this state lives in a plain dict, is NOT shared
across workers/instances, and would need real cross-process coordination
(Redis, etc.) before this deployment could scale to multiple processes —
exactly the same posture as ``live_session_repository.py``'s ownership
lock and ``core/persistence/repository.py``'s optimistic-concurrency
version column.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request


def client_ip(request: Request) -> str:
    """A stable per-client rate-limit key. Render (and every reverse proxy)
    terminates the real client's TCP connection itself, so
    ``request.client.host`` is the PROXY's address, not the candidate's —
    keying on that would put every user behind the same bucket. Use the
    first hop in ``X-Forwarded-For`` (the original client, per the
    standard left-to-right convention) when present, falling back to the
    direct peer for local/offline runs where there is no proxy at all.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


class SlidingWindowRateLimiter:
    """Allows at most ``max_events`` within a rolling ``window_sec`` window,
    independently per key. ``time_fn`` is injectable (matching
    ``live/guard.py``'s ``SessionGuard`` DI pattern) so tests never depend
    on wall-clock delays.
    """

    def __init__(
        self,
        max_events: int,
        window_sec: float,
        *,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._max_events = max_events
        self._window_sec = window_sec
        self._time = time_fn or time.monotonic
        # One deque per key, holding the monotonic timestamp of each recent
        # allowed event. Keys are never removed — see the module docstring's
        # single-process note; a real key space here is bounded by distinct
        # session_ids/IPs actually seen, not unbounded user input.
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        window = self._events[key]
        cutoff = self._time() - self._window_sec
        while window and window[0] < cutoff:
            window.popleft()
        return window

    def allow(self, key: str) -> bool:
        """Record an attempt for ``key`` and return whether it's within the
        limit. A rejected attempt is NOT recorded — repeatedly hammering a
        limited key doesn't reset or extend its own window.
        """
        window = self._prune(key)
        if len(window) >= self._max_events:
            return False
        window.append(self._time())
        return True

    def retry_after(self, key: str) -> float:
        """Seconds until the oldest event in ``key``'s current window
        expires (0.0 if the key isn't currently limited)."""
        window = self._prune(key)
        if len(window) < self._max_events:
            return 0.0
        return max(0.0, self._window_sec - (self._time() - window[0]))
