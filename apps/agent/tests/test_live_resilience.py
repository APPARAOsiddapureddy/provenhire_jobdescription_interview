"""Offline tests for live/resilience.py's retry/backoff + provider failover
(Phase 4 of the backend hardening plan).

Fully deterministic: sleep_fn/random_fn are injected fakes (matching
live/guard.py's SessionGuard time_fn DI pattern), so no test depends on
wall-clock delays or real randomness.
"""

from __future__ import annotations

import asyncio

import pytest

from proven_hire_agent.live.orchestrator import CompletionResult
from proven_hire_agent.live.resilience import default_is_retryable, make_resilient_complete_fn


def _run(coro):
    return asyncio.run(coro)


class _FakeSleeper:
    """Records every requested delay instead of actually sleeping."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _no_jitter() -> float:
    return 0.0


def test_resilient_complete_fn_succeeds_on_first_try_no_retry_no_failover() -> None:
    calls = {"primary": 0}

    async def primary(messages, tools):
        calls["primary"] += 1
        return CompletionResult(content="ok")

    fn = make_resilient_complete_fn([("primary", primary)], sleep_fn=_FakeSleeper(), random_fn=_no_jitter)
    result = _run(fn([], []))

    assert result.content == "ok"
    assert calls["primary"] == 1


def test_resilient_complete_fn_retries_a_retryable_error_then_succeeds() -> None:
    calls = {"n": 0}
    sleeper = _FakeSleeper()

    async def primary(messages, tools):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("slow provider")
        return CompletionResult(content="recovered")

    fn = make_resilient_complete_fn(
        [("primary", primary)], max_attempts=3, base_delay_sec=1.0, sleep_fn=sleeper, random_fn=_no_jitter
    )
    result = _run(fn([], []))

    assert result.content == "recovered"
    assert calls["n"] == 3
    # Two retries -> two sleeps, exponential: 1.0s then 2.0s (no jitter).
    assert sleeper.delays == [1.0, 2.0]


def test_resilient_complete_fn_on_retry_callback_fires_before_each_sleep() -> None:
    retries_seen: list[tuple[str, int]] = []

    async def on_retry(provider: str, attempt: int) -> None:
        retries_seen.append((provider, attempt))

    calls = {"n": 0}

    async def primary(messages, tools):
        calls["n"] += 1
        if calls["n"] < 2:
            raise TimeoutError("blip")
        return CompletionResult(content="ok")

    fn = make_resilient_complete_fn(
        [("primary", primary)],
        max_attempts=3,
        sleep_fn=_FakeSleeper(),
        random_fn=_no_jitter,
        on_retry=on_retry,
    )
    _run(fn([], []))

    assert retries_seen == [("primary", 1)]


def test_resilient_complete_fn_fails_over_to_the_next_provider_after_budget_exhausted() -> None:
    primary_calls = {"n": 0}
    fallback_calls = {"n": 0}

    async def primary(messages, tools):
        primary_calls["n"] += 1
        raise TimeoutError("primary always times out")

    async def fallback(messages, tools):
        fallback_calls["n"] += 1
        return CompletionResult(content="fallback saved the turn")

    fn = make_resilient_complete_fn(
        [("primary", primary), ("fallback", fallback)],
        max_attempts=2,
        sleep_fn=_FakeSleeper(),
        random_fn=_no_jitter,
    )
    result = _run(fn([], []))

    assert result.content == "fallback saved the turn"
    assert primary_calls["n"] == 2  # exhausted its own budget first
    assert fallback_calls["n"] == 1  # succeeded on the fallback's first try


def test_resilient_complete_fn_all_providers_exhausted_reraises_the_primarys_error() -> None:
    async def primary(messages, tools):
        raise TimeoutError("primary down")

    async def fallback(messages, tools):
        raise ConnectionError("fallback also down")

    fn = make_resilient_complete_fn(
        [("primary", primary), ("fallback", fallback)],
        max_attempts=1,
        sleep_fn=_FakeSleeper(),
        random_fn=_no_jitter,
    )

    with pytest.raises(TimeoutError, match="primary down"):
        _run(fn([], []))


def test_resilient_complete_fn_non_retryable_error_fails_over_immediately_without_retry() -> None:
    """A non-retryable error (e.g. a 400/401-class failure) must not burn
    the retry budget on the SAME provider — fail over to the next one right
    away."""
    primary_calls = {"n": 0}

    async def primary(messages, tools):
        primary_calls["n"] += 1
        raise ValueError("bad request — will never succeed on retry")

    async def fallback(messages, tools):
        return CompletionResult(content="fallback")

    fn = make_resilient_complete_fn(
        [("primary", primary), ("fallback", fallback)],
        max_attempts=5,
        is_retryable=lambda exc: isinstance(exc, (TimeoutError, ConnectionError)),
        sleep_fn=_FakeSleeper(),
        random_fn=_no_jitter,
    )
    result = _run(fn([], []))

    assert result.content == "fallback"
    assert primary_calls["n"] == 1  # no retries burned on a non-retryable error


def test_resilient_complete_fn_rejects_an_empty_provider_list() -> None:
    with pytest.raises(ValueError, match="at least one provider"):
        make_resilient_complete_fn([])


def test_default_is_retryable_classifies_timeouts_and_connection_errors_as_retryable() -> None:
    assert default_is_retryable(TimeoutError("slow")) is True
    assert default_is_retryable(ConnectionError("dropped")) is True


def test_default_is_retryable_classifies_generic_errors_as_not_retryable() -> None:
    assert default_is_retryable(ValueError("bad input")) is False
    assert default_is_retryable(KeyError("missing")) is False
