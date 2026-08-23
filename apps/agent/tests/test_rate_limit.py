"""Offline tests for core/rate_limit.py (Phase 5, backend hardening plan).

SlidingWindowRateLimiter is exercised with an injected fake clock (matching
live/guard.py's SessionGuard time_fn DI pattern) so nothing here depends on
wall-clock delays. client_ip is exercised against fake Request-shaped
objects (only .headers/.client are read) rather than constructing a real
Starlette Request.
"""

from __future__ import annotations

from proven_hire_agent.core.rate_limit import SlidingWindowRateLimiter, client_ip


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_allows_events_up_to_the_limit_within_the_window() -> None:
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(max_events=3, window_sec=60.0, time_fn=clock)

    assert limiter.allow("a") is True
    assert limiter.allow("a") is True
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False  # 4th within the same window is over budget


def test_rejected_attempts_are_not_recorded() -> None:
    """Hammering a limited key must not itself extend/reset the window —
    only successfully-allowed events count."""
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(max_events=1, window_sec=60.0, time_fn=clock)

    assert limiter.allow("a") is True
    for _ in range(5):
        assert limiter.allow("a") is False
    clock.advance(60.1)
    assert limiter.allow("a") is True  # the ORIGINAL single event has rolled off


def test_window_rolls_off_old_events() -> None:
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(max_events=2, window_sec=10.0, time_fn=clock)

    assert limiter.allow("a") is True
    clock.advance(5.0)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False  # both still within the last 10s

    clock.advance(5.1)  # the FIRST event (at t=0) is now outside the window
    assert limiter.allow("a") is True


def test_keys_are_independent() -> None:
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(max_events=1, window_sec=60.0, time_fn=clock)

    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    assert limiter.allow("b") is True  # a DIFFERENT key has its own budget


def test_retry_after_reports_zero_when_not_limited() -> None:
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(max_events=3, window_sec=60.0, time_fn=clock)
    limiter.allow("a")
    assert limiter.retry_after("a") == 0.0
    assert limiter.retry_after("never-seen") == 0.0


def test_retry_after_counts_down_to_the_oldest_events_expiry() -> None:
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(max_events=1, window_sec=10.0, time_fn=clock)
    limiter.allow("a")
    limiter.allow("a")  # rejected, doesn't change the window

    assert limiter.retry_after("a") == 10.0
    clock.advance(4.0)
    assert limiter.retry_after("a") == 6.0


# --- client_ip -----------------------------------------------------------


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, headers: dict[str, str], client_host: str | None) -> None:
        self.headers = headers
        self.client = _FakeClient(client_host) if client_host is not None else None


def test_client_ip_prefers_the_first_x_forwarded_for_hop() -> None:
    req = _FakeRequest({"x-forwarded-for": "203.0.113.5, 10.0.0.1, 10.0.0.2"}, "10.0.0.2")
    assert client_ip(req) == "203.0.113.5"


def test_client_ip_falls_back_to_the_direct_peer_with_no_proxy_header() -> None:
    req = _FakeRequest({}, "127.0.0.1")
    assert client_ip(req) == "127.0.0.1"


def test_client_ip_handles_a_missing_client_entirely() -> None:
    req = _FakeRequest({}, None)
    assert client_ip(req) == "unknown"


def test_client_ip_ignores_a_blank_forwarded_header() -> None:
    req = _FakeRequest({"x-forwarded-for": ""}, "127.0.0.1")
    assert client_ip(req) == "127.0.0.1"
