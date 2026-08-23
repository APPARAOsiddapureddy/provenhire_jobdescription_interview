"""LLM completion resilience: retry/backoff + provider failover (Phase 4 of
the live-interview backend hardening plan).

Today, ANY transient LLM failure (a 429, a timeout, a 5xx) reaches
``orchestrator.py``'s ``_run_loop`` uncaught, propagates through ``run_turn``/
``open_interview``, and is caught only by ``api/live.py``'s outermost
``except Exception`` — which ends the whole interview. Phase 3's reconnect
work makes that *recoverable*, but the candidate still experiences a jarring
disconnect over what is often a one-off blip.

``make_resilient_complete_fn`` wraps a chain of provider ``CompleteFn``s
(primary first, then any other fully-configured live provider as fallback —
see ``api/live.py``'s ``_make_live_complete_fn``) into ONE ``CompleteFn`` with
the exact same signature, so nothing downstream (``orchestrator.py``, tests)
needs to know resilience is happening at all. Each provider gets its own
bounded retry budget with exponential backoff + jitter before failing over to
the next; the primary provider's last exception is what's re-raised if every
provider is exhausted — matching ``core/adapters/llm.py``'s ``FallbackLLM``
rationale (a caller's failure log should name the actually-configured
provider, not whichever fallback happened to fail last).

DI-for-testability, matching ``live/guard.py``'s ``SessionGuard`` shape: real
wall-clock sleep and real jitter are the defaults, both injectable so retry
tests never depend on wall-clock delays or need to mock ``random``.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any

from ..core.logging import get_logger
from .orchestrator import CompleteFn, CompletionResult

log = get_logger(__name__)

# Bounded PER-PROVIDER retry budget — not unlimited, and every attempt
# (including the first) counts against it. 3 attempts x up to ~8s backoff is
# a handful of seconds added to a turn in the worst case, not minutes.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY_SEC = 0.5
DEFAULT_MAX_DELAY_SEC = 8.0


def default_is_retryable(exc: BaseException) -> bool:
    """Retryable = transient, worth trying again: timeouts, connection
    errors, rate limits, and the provider's own 5xx. NOT retryable: bad
    request / auth / permission errors, which fail identically on every
    retry and would only burn the retry budget and delay a real error
    reaching the caller.
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    try:
        import openai
    except ImportError:  # pragma: no cover - the openai SDK is always
        # installed wherever this module is actually reachable (the live
        # WS route requires it); tests exercise this path with fakes.
        return False
    return isinstance(
        exc,
        (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.InternalServerError,
        ),
    )


async def _complete_with_retry(
    complete_fn: CompleteFn,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    max_attempts: int,
    base_delay_sec: float,
    max_delay_sec: float,
    is_retryable: Callable[[BaseException], bool],
    sleep_fn: Callable[[float], Awaitable[None]],
    random_fn: Callable[[], float],
    provider_name: str,
    on_retry: Callable[[str, int], Awaitable[None]] | None,
) -> CompletionResult:
    """Retry ONE provider's CompleteFn with bounded exponential backoff +
    jitter. Re-raises the last exception once the attempt budget is spent or
    a non-retryable error is hit — the caller (make_resilient_complete_fn's
    closure) decides whether to fail over to another provider.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return await complete_fn(messages, tools)
        except Exception as exc:
            if attempt >= max_attempts or not is_retryable(exc):
                raise
            delay = min(max_delay_sec, base_delay_sec * (2 ** (attempt - 1)))
            delay += delay * random_fn() * 0.5  # up to +50% jitter
            log.warning(
                "live: %s completion attempt %d/%d failed (%s); retrying in %.2fs",
                provider_name,
                attempt,
                max_attempts,
                exc,
                delay,
            )
            if on_retry is not None:
                await on_retry(provider_name, attempt)
            await sleep_fn(delay)
    raise AssertionError("unreachable: the loop above always returns or raises")  # pragma: no cover


def make_resilient_complete_fn(
    providers: list[tuple[str, CompleteFn]],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay_sec: float = DEFAULT_BASE_DELAY_SEC,
    max_delay_sec: float = DEFAULT_MAX_DELAY_SEC,
    is_retryable: Callable[[BaseException], bool] = default_is_retryable,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_fn: Callable[[], float] = random.random,
    on_retry: Callable[[str, int], Awaitable[None]] | None = None,
) -> CompleteFn:
    """Wrap ``providers`` (``(name, CompleteFn)`` pairs, primary first) into
    ONE ``CompleteFn``. Each provider is retried up to ``max_attempts``
    times with backoff+jitter; if a provider's budget is exhausted (or its
    first error isn't retryable at all), the NEXT provider in the list is
    tried fresh. Raises the primary's last exception if every provider
    fails.

    ``on_retry(provider_name, attempt)`` — if supplied — is awaited right
    before each retry's sleep (not on failover to a new provider), so a
    caller with a live connection (the WS handler) can tell the candidate
    something is being retried instead of the connection just going quiet.
    """
    if not providers:
        raise ValueError("make_resilient_complete_fn needs at least one provider")

    async def resilient_complete(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> CompletionResult:
        primary_exc: BaseException | None = None
        for name, fn in providers:
            try:
                return await _complete_with_retry(
                    fn,
                    messages,
                    tools,
                    max_attempts=max_attempts,
                    base_delay_sec=base_delay_sec,
                    max_delay_sec=max_delay_sec,
                    is_retryable=is_retryable,
                    sleep_fn=sleep_fn,
                    random_fn=random_fn,
                    provider_name=name,
                    on_retry=on_retry,
                )
            except Exception as exc:  # noqa: BLE001 - try the next provider, if any
                if primary_exc is None:
                    primary_exc = exc
                log.warning(
                    "live: provider %s exhausted its retry budget; trying the next "
                    "fallback if one is configured",
                    name,
                )
                continue
        assert primary_exc is not None  # providers is non-empty (checked above)
        raise primary_exc

    return resilient_complete
