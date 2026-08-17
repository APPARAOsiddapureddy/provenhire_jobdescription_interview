"""Follow-up depth budgets: pure, LLM-free lookup for how many live follow-up
probes each round's questions should be seeded with (see docs/QUESTION_GENERATION_SPEC.md
and ``role_packs.py`` for the sibling deterministic-selection pattern this follows).

Like ``role_packs.py``, every function here is a deterministic function of its
inputs — no network, no LLM, no randomness — so a round's prompt builder and its
node's post-call pin (``nodes.py``) always agree on the same budget for a given
``PrepRequest.follow_up_depth``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..shared_models import FollowUpDepth


@dataclass(frozen=True)
class FollowUpBudget:
    """Per-round follow-up counts for one depth setting."""

    technical_followups: int
    """Followups seeded on each ORDINARY general-round technical question."""
    signature_followups_min: int
    signature_followups_max: int
    """Followup-tree size range for the general round's signature/case question."""
    coding_followups: int
    """Followups (hints) seeded on the coding round's single problem."""
    behavioral_followups: int
    """Followups seeded on each behavioral-round question."""


# "moderate" reproduces this project's original hard-coded behavior exactly (1
# ordinary technical follow-up, a 4-8 entry signature tree, 1 coding hint, 1
# behavioral probe) — so a PrepRequest that omits follow_up_depth is unaffected.
_BUDGETS: dict[FollowUpDepth, FollowUpBudget] = {
    "light": FollowUpBudget(
        technical_followups=0,
        signature_followups_min=2,
        signature_followups_max=4,
        coding_followups=1,
        behavioral_followups=1,
    ),
    "moderate": FollowUpBudget(
        technical_followups=1,
        signature_followups_min=4,
        signature_followups_max=8,
        coding_followups=1,
        behavioral_followups=1,
    ),
    "deep": FollowUpBudget(
        technical_followups=2,
        signature_followups_min=6,
        signature_followups_max=10,
        coding_followups=2,
        behavioral_followups=2,
    ),
}


def followup_budget(depth: FollowUpDepth) -> FollowUpBudget:
    """The ``FollowUpBudget`` for ``depth`` (falls back to "moderate" for an
    unrecognized value, matching the field's own Pydantic default).
    """
    return _BUDGETS.get(depth, _BUDGETS["moderate"])
