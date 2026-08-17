"""Offline tests for the shared quality-band rubric text and its wiring into
the WP-7 scoring prompts — the "can't disagree" contract: grading, adversarial
re-scoring, and sample-answer generation must all quote the identical block.
"""

from __future__ import annotations

from proven_hire_agent.post.prompts import (
    evaluate_answer_prompts,
    model_answer_prompts,
    verify_score_prompts,
)
from proven_hire_agent.post.rubric import quality_band_block
from proven_hire_agent.shared_models import CandidateProfile, PlannedQuestion


def test_quality_band_block_has_five_bands() -> None:
    block = quality_band_block()
    assert isinstance(block, str) and block
    # One line per band, e.g. "- 0-1: ...", "- 4-5 (strong ...): ...".
    for band in ("0-1", "1-2", "2-3", "3-4", "4-5"):
        assert band in block


def _question() -> PlannedQuestion:
    return PlannedQuestion(
        id="q1",
        section="technical",
        text={"en": "Explain how you'd debug a slow query."},
        difficulty=3,
        rubric=[],
        followups=[],
        target_competency="databases",
    )


def _candidate() -> CandidateProfile:
    return CandidateProfile(
        name="Jordan",
        headline="Backend engineer",
        summary_120w="Backend engineer with API experience.",
        years_experience=4,
        seniority="mid",
        skills=["python"],
        projects=[],
        achievements=[],
        education=[],
        spoken_languages=["en"],
    )


def test_evaluate_and_verify_prompts_quote_the_same_block() -> None:
    block = quality_band_block()
    eval_system, _ = evaluate_answer_prompts(_question(), "some answer")
    verify_system, _ = verify_score_prompts("databases", "evidence", 3.0, "transcript")
    assert block in eval_system
    assert block in verify_system


def test_model_answer_prompt_quotes_the_same_block_and_targets_strong_band() -> None:
    block = quality_band_block()
    system, _ = model_answer_prompts(_question(), _candidate(), None)
    assert block in system
    assert "STRONG" in system
    assert "hollow" in system.lower()
