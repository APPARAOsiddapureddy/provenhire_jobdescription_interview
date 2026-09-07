"""Unit tests for assemble_plan's deterministic quality pass (dedup, rubric
weight normalization, technical difficulty repair, competency coverage).

These test the pure functions directly rather than through run_prep, since
they are deterministic logic with no LLM/network dependency of their own —
the MockLLM path in test_prep.py already exercises them indirectly, but
direct tests pin the actual behavior each function is responsible for.
"""

from __future__ import annotations

import asyncio

from proven_hire_agent.core.deps import build_deps
from proven_hire_agent.prep import nodes
from proven_hire_agent.prep.state import PrepState
from proven_hire_agent.shared_models import (
    JobSpec,
    LanguageMode,
    PlannedQuestion,
    PrepRequest,
    RubricItem,
)


def _q(
    id_: str,
    text: str,
    *,
    section: str = "technical",
    difficulty: int = 1,
    target_competency: str = "system_design",
    rubric: list[RubricItem] | None = None,
) -> PlannedQuestion:
    return PlannedQuestion(
        id=id_,
        section=section,
        text={"en": text},
        difficulty=difficulty,
        rubric=rubric or [],
        followups=[],
        target_competency=target_competency,
    )


def test_dedupe_keeps_first_occurrence_and_drops_near_duplicate() -> None:
    a = _q("gen-1", "Tell me about a challenging project you worked on.", section="behavioral")
    b = _q("gen-2", "Tell me about a project you worked on that was challenging.", section="technical")
    kept, dropped = nodes._dedupe_questions([a, b], "en")

    assert [q.id for q in kept] == ["gen-1"]
    assert len(dropped) == 1
    assert "gen-2" in dropped[0]


def test_dedupe_leaves_genuinely_different_questions_alone() -> None:
    a = _q("gen-1", "How would you design a rate limiter for our public API?")
    b = _q("gen-2", "Walk me through how you'd debug a memory leak in production.")
    kept, dropped = nodes._dedupe_questions([a, b], "en")

    assert [q.id for q in kept] == ["gen-1", "gen-2"]
    assert dropped == []


def test_dedupe_does_not_flag_different_topics_sharing_a_scaffold() -> None:
    # Both start "Tell me about a time you..." — a shared behavioral
    # scaffold — but ask about genuinely different things. Character-level
    # similarity (the metric originally tried here) scored this pair HIGHER
    # than the actual reordered duplicate above, which would have been a
    # false positive; word-overlap correctly leaves both in place.
    a = _q(
        "gen-1",
        "Tell me about a time you led a project under a tight deadline.",
        section="behavioral",
    )
    b = _q(
        "gen-2",
        "Tell me about a time you mentored a junior engineer.",
        section="behavioral",
    )
    kept, dropped = nodes._dedupe_questions([a, b], "en")

    assert [q.id for q in kept] == ["gen-1", "gen-2"]
    assert dropped == []


def test_normalize_rubric_weights_rescales_to_sum_to_one() -> None:
    q = _q(
        "gen-1",
        "Explain your indexing strategy.",
        rubric=[
            RubricItem(criterion="depth", weight=0.8, description="d"),
            RubricItem(criterion="clarity", weight=0.8, description="c"),
        ],
    )
    [fixed] = nodes._normalize_rubric_weights([q])

    total = sum(item.weight for item in fixed.rubric)
    assert abs(total - 1.0) < 1e-9
    # Relative emphasis preserved: both items were equal before, still equal after.
    assert fixed.rubric[0].weight == fixed.rubric[1].weight


def test_normalize_rubric_weights_leaves_already_correct_alone() -> None:
    q = _q(
        "gen-1",
        "Explain your indexing strategy.",
        rubric=[
            RubricItem(criterion="depth", weight=0.6, description="d"),
            RubricItem(criterion="clarity", weight=0.4, description="c"),
        ],
    )
    [fixed] = nodes._normalize_rubric_weights([q])

    assert fixed.rubric[0].weight == 0.6
    assert fixed.rubric[1].weight == 0.4


def test_normalize_rubric_weights_handles_all_zero_without_crashing() -> None:
    q = _q(
        "gen-1",
        "Explain your indexing strategy.",
        rubric=[
            RubricItem(criterion="depth", weight=0.0, description="d"),
            RubricItem(criterion="clarity", weight=0.0, description="c"),
        ],
    )
    [fixed] = nodes._normalize_rubric_weights([q])

    assert fixed.rubric[0].weight == 0.5
    assert fixed.rubric[1].weight == 0.5


def test_reorder_technical_by_difficulty_sorts_all_but_the_signature_question() -> None:
    # Model returned difficulty out of order: 4, 1, 3 — with the signature
    # question (last) at difficulty 2, which must NOT move.
    q1 = _q("gen-1", "Q1", difficulty=4)
    q2 = _q("gen-2", "Q2", difficulty=1)
    q3 = _q("gen-3", "Q3", difficulty=3)
    signature = _q("gen-4", "Signature question", difficulty=2)
    ordered = nodes._reorder_technical_by_difficulty([q1, q2, q3, signature])

    assert [q.id for q in ordered] == ["gen-2", "gen-3", "gen-1", "gen-4"]
    assert ordered[-1].id == "gen-4"  # signature question still last


def test_reorder_technical_by_difficulty_ignores_non_technical_sections() -> None:
    intro = _q("gen-0", "Warm-up", section="intro", difficulty=1)
    q1 = _q("gen-1", "Q1", difficulty=3)
    q2 = _q("gen-2", "Q2", difficulty=1)
    ordered = nodes._reorder_technical_by_difficulty([intro, q1, q2])

    # Only one technical question after the intro-excluded pair? No — two
    # technical questions means the LAST (q2) is the signature question and
    # stays put; q1 alone is "reorderable" but there's nothing to sort it
    # against, so it's unchanged too.
    assert [q.id for q in ordered] == ["gen-0", "gen-1", "gen-2"]


def test_check_competency_coverage_flags_shortfall() -> None:
    questions = [_q("gen-1", "Q1", target_competency="apis")]
    warnings = nodes._check_competency_coverage(
        questions, {"apis": 1, "data_modeling": 2}
    )

    assert len(warnings) == 1
    assert "data_modeling" in warnings[0]
    assert "apis" not in warnings[0]


def test_check_competency_coverage_silent_when_met() -> None:
    questions = [
        _q("gen-1", "Q1", target_competency="apis"),
        _q("gen-2", "Q2", target_competency="apis"),
    ]
    warnings = nodes._check_competency_coverage(questions, {"apis": 2})

    assert warnings == []


def _job() -> JobSpec:
    return JobSpec(
        title="Senior Backend Engineer",
        company_name="ExampleCorp",
        seniority="senior",
        must_have=["Python"],
        nice_to_have=[],
        responsibilities=["Build APIs"],
        tech_stack=["Python", "Postgres"],
        raw_text="",
    )


def test_assemble_plan_applies_dedup_for_a_plausible_overlap() -> None:
    """A single real cross-round overlap should be removed normally."""
    state: PrepState = {
        "req": PrepRequest(
            cv_url="https://example.com/cv.pdf",
            jd_text="x",
            company="ExampleCorp",
            language_mode=LanguageMode(primary="en", mixed=False),
            follow_up_depth="moderate",
        ),
        "general_questions": [
            _q("gen-1", "Warm-up", section="intro", difficulty=1),
            _q("gen-2", "Design a rate limiter for our API.", difficulty=2),
            _q("gen-3", "Wrap-up", section="wrap", difficulty=1),
        ],
        "behavioral_questions": [
            _q(
                "beh-1",
                "Tell me about a challenging project you worked on.",
                section="behavioral",
                difficulty=2,
            ),
            _q(
                "beh-2",
                "Tell me about a project you worked on that was challenging.",
                section="behavioral",
                difficulty=2,
            ),
        ],
        "coding_questions": [_q("code-1", "Solve a coding problem.", section="coding")],
        "technical_counts": {},
    }
    result = asyncio.run(nodes.assemble_plan(state, build_deps()))

    behavioral = [q for q in result["plan"].questions if q.section == "behavioral"]
    assert len(behavioral) == 1, "the real duplicate should have been removed"


def test_assemble_plan_skips_dedup_rather_than_emptying_a_section() -> None:
    """Degenerate input (every question textually identical, as MockLLM
    produces) must never zero out a whole section — this is the exact bug
    caught by test_prep.py's MockLLM run and is worth pinning directly."""
    state: PrepState = {
        "req": PrepRequest(
            cv_url="https://example.com/cv.pdf",
            jd_text="x",
            company="ExampleCorp",
            language_mode=LanguageMode(primary="en", mixed=False),
            follow_up_depth="moderate",
        ),
        "general_questions": [
            _q("gen-1", "mock", section="intro"),
            _q("gen-2", "mock", section="technical"),
            _q("gen-3", "mock", section="wrap"),
        ],
        "behavioral_questions": [_q("beh-1", "mock", section="behavioral")],
        "coding_questions": [_q("code-1", "mock", section="coding")],
        "technical_counts": {},
    }
    result = asyncio.run(nodes.assemble_plan(state, build_deps()))

    sections_present = {q.section for q in result["plan"].questions}
    assert sections_present == {"intro", "technical", "wrap", "behavioral", "coding"}
    assert len(result["plan"].questions) == 5, "dedup should have been skipped entirely"
