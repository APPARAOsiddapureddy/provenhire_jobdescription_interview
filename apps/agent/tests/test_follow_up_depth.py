"""Offline tests for the pure follow-up-depth lookup table."""

from __future__ import annotations

from proven_hire_agent.prep.follow_up_depth import followup_budget


def test_moderate_matches_original_hard_coded_behavior() -> None:
    """"moderate" is the default and must reproduce this project's pre-existing
    hard-coded constants exactly (1 ordinary technical followup, a 4-8 entry
    signature tree, 1 coding hint, 1 behavioral probe) — backward compat for
    any PrepRequest that omits follow_up_depth.
    """
    budget = followup_budget("moderate")
    assert budget.technical_followups == 1
    assert (budget.signature_followups_min, budget.signature_followups_max) == (4, 8)
    assert budget.coding_followups == 1
    assert budget.behavioral_followups == 1


def test_light_is_strictly_less_than_moderate() -> None:
    light = followup_budget("light")
    moderate = followup_budget("moderate")
    assert light.technical_followups < moderate.technical_followups
    assert light.signature_followups_max < moderate.signature_followups_max
    assert light.behavioral_followups <= moderate.behavioral_followups


def test_deep_is_strictly_more_than_moderate() -> None:
    deep = followup_budget("deep")
    moderate = followup_budget("moderate")
    assert deep.technical_followups > moderate.technical_followups
    assert deep.signature_followups_max > moderate.signature_followups_max
    assert deep.coding_followups > moderate.coding_followups
    assert deep.behavioral_followups > moderate.behavioral_followups


def test_unknown_depth_falls_back_to_moderate() -> None:
    assert followup_budget("nonsense") == followup_budget("moderate")  # type: ignore[arg-type]
