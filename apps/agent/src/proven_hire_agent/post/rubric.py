"""Shared, verbatim answer-quality rubric text for the WP-7 scoring pipeline.

:func:`quality_band_block` is spliced verbatim into every prompt that reasons
about answer quality on the project's 0-5 scale — grading
(``evaluate_answer_prompts``), adversarial re-scoring (``verify_score_prompts``),
and sample-answer generation (``model_answer_prompts``) — so a grader and a
sample-answer writer can never silently disagree about what a "4" looks like.

The band THRESHOLDS here are prose only, describing the sub-ranges of the
existing 0-5 scale; the authoritative numeric boundaries (which score maps to
which named band: weak/developing/solid/strong) stay in
``post/evaluator.py``'s ``level_for_score`` and are NOT duplicated or
re-derived here.
"""

from __future__ import annotations


def quality_band_block() -> str:
    """Verbatim 0-5 answer-quality band definitions, reused across every
    scoring-adjacent prompt so grading and sample answers can't disagree.
    """
    return (
        "SCORE BANDS (0-5 scale — use these, don't invent your own):\n"
        "- 0-1: Off-topic, restates the question back, or names no concrete "
        "mechanism or example relevant to the rubric.\n"
        "- 1-2: Names a relevant term or concept but never explains how or why "
        "it applies here; no concrete example, mostly guessing.\n"
        "- 2-3 (developing): Correct direction with a real mechanism or "
        "example, but leaves a gap a rigorous interviewer would probe (a "
        "missing trade-off, an unhandled edge case, or no 'why').\n"
        "- 3-4 (solid): Concrete and mostly correct, backed by a specific "
        "example, AND names at least one trade-off or edge case unprompted.\n"
        "- 4-5 (strong — never a hollow perfect score): Concrete, correct, "
        "and demonstrates judgment: names the trade-off space, anticipates a "
        "failure mode, ties it to a real decision. Reserve 5 for an answer "
        "with no meaningful gap a rigorous interviewer would push on."
    )
