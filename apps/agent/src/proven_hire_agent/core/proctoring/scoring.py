"""Pure weighted-decay strike scoring — deliberately NOT a repository method.

Kept as a standalone pure function (list of events + a clock in, a number
out) rather than living on ``ProctoringEventsRepository`` because: (1) the
repository protocol has two implementations (Memory, Supabase) and this
math has nothing to do with storage — putting it there means either
duplicating it or importing it into one from the other, which just hides
a pure function inside an I/O class; (2) two independent call sites need
it (the POST /api/proctoring-events handler, for the immediate ban check,
and the session-resume path in api/session.py) — a plain function importable
from both avoids a second copy; (3) it is trivially unit-testable with no
FastAPI app, no repository, no I/O at all.

Decay model: PER-EVENT, not aggregate. Each event's own weight decays by
``decay_per_step`` for every ``decay_step_sec`` elapsed since ITS OWN
``created_at``, floored at 0, then all events are summed fresh on every
call — nothing is decremented in storage, so "recomputed retroactively
from elapsed time" (including after a reload) falls out for free.
"""

from __future__ import annotations

import math
from datetime import datetime

from ...shared_models import ProctoringEvent
from .weights import ProctoringWeights, load_proctoring_weights


def _parse_created_at(value: str) -> datetime:
    # Postgres/Supabase may return a trailing "Z"; fromisoformat wants
    # "+00:00" on Python versions before 3.11's relaxed parser.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def compute_strike_score(
    events: list[ProctoringEvent],
    *,
    now: datetime,
    weights: ProctoringWeights | None = None,
) -> float:
    """Sum of per-event decayed weight, recomputed fresh from raw events."""
    w = weights or load_proctoring_weights()
    total = 0.0
    for event in events:
        base = w.weight_for(event.type)
        if base <= 0:
            continue  # logged-only event types never contribute to score
        created_at = _parse_created_at(event.created_at)
        elapsed_sec = max(0.0, (now - created_at).total_seconds())
        steps = math.floor(elapsed_sec / w.decay_step_sec)
        decayed = max(0.0, base - steps * w.decay_per_step)
        total += decayed
    return total


def is_ban_threshold_exceeded(score: float, threshold: float | None = None) -> bool:
    w = load_proctoring_weights()
    effective_threshold = threshold if threshold is not None else w.strike_threshold_default
    return score >= effective_threshold
