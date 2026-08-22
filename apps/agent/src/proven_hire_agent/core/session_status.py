"""The canonical interview session status model — one place, shared by every
layer that needs to reason about it.

Moved out of ``api/views.py``/``api/session.py`` (Phase 0 of the live-interview
backend hardening plan) because the persistence layer (``core/persistence/
repository.py``, Phase 2) needs to enforce the SAME terminal-state and
transition rules the API route currently enforces on its own, and ``core/``
must not import from ``api/`` (that's backwards — routes depend on core
services, not the other way around). This module is the single source of
truth both layers import from.

Canonical status lifecycle, grounded in every real ``update_status(...)`` call
site in the codebase today (not aspirational — this documents what the system
actually does):

    prep --[input validation fails]--> rejected          (terminal)
    prep --[prep pipeline raises]-----> error             (terminal)
    prep --[prep succeeds]------------> ready
    ready --[live: save_context fails]-> error            (terminal)
    ready --[live: no substantive answers captured]-> no_answers  (terminal)
    ready --[live: real answers persisted]-> ready         (unchanged; scoring runs next)
    ready --[scoring succeeds]---------> complete          (terminal)

``rejected``/``error``/``complete``/``no_answers`` are final: once reached, no
further write should be able to move the session to a different status or
mutate its context/transcript/scorecard. This is currently enforced ONLY in
``api/session.py``'s ``post_live_result`` route, via a read-then-write race
(see Phase 2) — the goal of moving these constants here is to let the
repository layer enforce the same graph atomically, for every caller.
"""

from __future__ import annotations

from typing import Literal

# "complete" is the terminal state set by the post-interview scoring step
# (see post/__init__.py); it must be a valid status or GET /api/session/{id}
# 500s on any read after an interview ends, blocking re-joins.
# "no_answers" is a terminal state for a session whose interview produced no
# answers (ended without answering, or answers never persisted) — scoring is
# skipped and no blank scorecard is written, so the UI can show an honest empty
# state instead of a misleading all-zeros report.
SessionStatus = Literal["prep", "ready", "rejected", "error", "complete", "no_answers"]

# Once a session reaches one of these it is done; a late/replayed write must
# never be able to overwrite a finished interview's history. Enforced today
# only in api/session.py's post_live_result route (a TOCTOU race — see the
# Phase 2 plan); Phase 2 moves enforcement into the repository layer itself so
# EVERY write path (both live transports, the HTTP route, the in-process
# persistence fallback) gets the same atomic protection.
TERMINAL_STATUSES: frozenset[str] = frozenset({"complete", "no_answers", "error", "rejected"})

# Statuses a live interview's own write-back path is allowed to set directly
# (as opposed to "complete", which only the scoring pipeline sets, or
# "rejected", which only prep-time validation sets).
ALLOWED_LIVE_STATUSES: frozenset[str] = frozenset({"no_answers", "error"})

# The legal transition graph: for each status, the set of statuses a write is
# allowed to move it TO. A terminal status maps to an empty set — no outbound
# transition is ever legal once reached. "ready" mapping to itself models the
# real repeated-write case (a live session's own sequential checkpoints keep
# the status at "ready" while cursor/answers advance).
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "prep": frozenset({"ready", "rejected", "error"}),
    "ready": frozenset({"ready", "error", "no_answers", "complete"}),
    "rejected": frozenset(),
    "error": frozenset(),
    "complete": frozenset(),
    "no_answers": frozenset(),
}


def is_transition_allowed(current: str, target: str) -> bool:
    """Whether moving from ``current`` to ``target`` is a legal transition.

    Unknown current statuses (should never happen given the schema, but this
    is a boundary function) are treated as NOT allowing any transition —
    fail closed, not open.
    """
    return target in VALID_TRANSITIONS.get(current, frozenset())
