"""The coordination WebSocket's client-facing event contract.

Phase 0 of the live-interview backend hardening plan. Today (before this
module existed) ``api/live.py`` only ever sent two shapes, built as inline
dict literals at each call site: ``{"type": "speak", "text": ...}`` and
``{"type": "error", "message": ...}``. Later phases add real failure modes
that need their OWN client-visible signal instead of the candidate just
watching the room sit there — a session that's resuming rather than starting
fresh (Phase 3), a transient LLM failure being retried (Phase 4), a rejected
oversized/rate-limited message (Phase 5), a second connection being refused
(Phase 3). Defining every shape here NOW means each of those phases implements
against one contract instead of inventing its own one-off dict shape.

This module only defines and constructs event payloads — it does not decide
WHEN to send them (that's each phase's own logic) and does not change any
CURRENT behavior: the existing ``speak``/``error`` call sites in ``api/live.py``
are updated to build their dicts through :func:`speak`/:func:`error` here
instead of an inline literal, with byte-identical wire output.

The browser's own ``CoordinationMessage`` type (``apps/web/lib/interview-
session.ts``) already tolerates unrecognized ``type`` values via a catch-all
branch, so adding new event types here is safe to do ahead of the client
actually handling them — the client just ignores what it doesn't recognize
yet, same as it does today for anything that isn't literally "speak"/"error".
"""

from __future__ import annotations

from typing import Literal, TypedDict

ClientEventType = Literal[
    # Already emitted today.
    "speak",
    "error",
    # Phase 3 — session lifecycle / reconnect.
    "session_connected",
    "session_resumed",
    "session_conflict",
    # Phase 4 — LLM/tool resilience.
    "llm_retrying",
    "temporary_failure",
    # Phase 5 — ingress hardening.
    "rate_limited",
    "input_rejected",
    # Phase 2/8 — persistence visibility.
    "checkpoint_failed",
]


class SpeakEvent(TypedDict):
    type: Literal["speak"]
    text: str


class ErrorEvent(TypedDict):
    type: Literal["error"]
    message: str
    # Machine-readable reason, additive — absent from today's wire shape, so
    # existing client parsing (which only reads .text/.message) is unaffected.
    code: str | None


class SessionConnectedEvent(TypedDict):
    type: Literal["session_connected"]


class SessionResumedEvent(TypedDict):
    type: Literal["session_resumed"]


class SessionConflictEvent(TypedDict):
    type: Literal["session_conflict"]
    message: str


class LlmRetryingEvent(TypedDict):
    type: Literal["llm_retrying"]
    attempt: int


class TemporaryFailureEvent(TypedDict):
    type: Literal["temporary_failure"]
    message: str


class RateLimitedEvent(TypedDict):
    type: Literal["rate_limited"]
    retry_after_sec: float


class InputRejectedEvent(TypedDict):
    type: Literal["input_rejected"]
    reason: str


class CheckpointFailedEvent(TypedDict):
    type: Literal["checkpoint_failed"]


def speak(text: str) -> SpeakEvent:
    return {"type": "speak", "text": text}


def error(message: str, *, code: str | None = None) -> ErrorEvent:
    """Candidate-safe by convention, not by enforcement here — callers must
    pass a generic, non-internal message (see Phase 6's error-sanitization
    sweep). ``code`` is for client-side branching, never shown to the user."""
    return {"type": "error", "message": message, "code": code}


def session_connected() -> SessionConnectedEvent:
    return {"type": "session_connected"}


def session_resumed() -> SessionResumedEvent:
    return {"type": "session_resumed"}


def session_conflict(message: str) -> SessionConflictEvent:
    return {"type": "session_conflict", "message": message}


def llm_retrying(attempt: int) -> LlmRetryingEvent:
    return {"type": "llm_retrying", "attempt": attempt}


def temporary_failure(message: str) -> TemporaryFailureEvent:
    return {"type": "temporary_failure", "message": message}


def rate_limited(retry_after_sec: float) -> RateLimitedEvent:
    return {"type": "rate_limited", "retry_after_sec": retry_after_sec}


def input_rejected(reason: str) -> InputRejectedEvent:
    return {"type": "input_rejected", "reason": reason}


def checkpoint_failed() -> CheckpointFailedEvent:
    return {"type": "checkpoint_failed"}
