"""Unit tests for _format_projects and its use in general/coding/behavioral
prompts. Pure string-building logic with no LLM/network dependency.
"""

from __future__ import annotations

from proven_hire_agent.prep.prompts import (
    _MAX_PROJECT_DESC_CHARS,
    _MAX_PROJECTS_IN_PROMPT,
    _format_projects,
    behavioral_round_prompts,
    coding_round_prompts,
    general_round_prompts,
)
from proven_hire_agent.shared_models import (
    CandidateProfile,
    CompanyIntel,
    GapAnalysis,
    JobSpec,
    LanguageMode,
    Project,
)


def _candidate(projects: list[Project]) -> CandidateProfile:
    return CandidateProfile(
        name="Jordan",
        headline="Senior Backend Engineer",
        summary_120w="x" * 10,
        years_experience=6,
        seniority="senior",
        skills=["Python"],
        projects=projects,
        achievements=[],
        education=[],
        spoken_languages=["en"],
    )


def _job() -> JobSpec:
    return JobSpec(
        title="Senior Backend Engineer",
        company_name="Acme",
        seniority="senior",
        must_have=["Python"],
        nice_to_have=[],
        responsibilities=["Build APIs"],
        tech_stack=["Python", "Postgres"],
        raw_text="",
    )


def test_format_projects_empty_list() -> None:
    assert _format_projects(_candidate([])) == "(none listed)"


def test_format_projects_includes_name_tech_and_description() -> None:
    p = Project(name="Checkout Cache", description="A caching layer.", tech=["Redis", "Python"])
    text = _format_projects(_candidate([p]))
    assert "Checkout Cache" in text
    assert "[Redis, Python]" in text
    assert "A caching layer." in text


def test_format_projects_caps_count() -> None:
    projects = [
        Project(name=f"Project {i}", description="d", tech=[]) for i in range(_MAX_PROJECTS_IN_PROMPT + 3)
    ]
    text = _format_projects(_candidate(projects))
    for i in range(_MAX_PROJECTS_IN_PROMPT):
        assert f"Project {i}" in text
    for i in range(_MAX_PROJECTS_IN_PROMPT, _MAX_PROJECTS_IN_PROMPT + 3):
        assert f"Project {i}" not in text


def test_format_projects_truncates_long_descriptions() -> None:
    p = Project(name="P", description="x" * 500, tech=[])
    text = _format_projects(_candidate([p]))
    # "P [] — " prefix plus at most _MAX_PROJECT_DESC_CHARS of description.
    assert text.count("x") == _MAX_PROJECT_DESC_CHARS


def test_general_round_prompts_include_candidate_projects() -> None:
    p = Project(name="Checkout Cache", description="Cut p99 latency.", tech=["Redis"])
    candidate = _candidate([p])
    job = _job()
    company = CompanyIntel(
        name="Acme", summary="s", tech_stack=[], values=[], interview_process=[],
        recent_news=[], citations=[],
    )
    gap = GapAnalysis(strengths=[], gaps=[], probe_targets=[], matched_skills=[], missing_skills=[], summary="")
    from proven_hire_agent.prep.follow_up_depth import followup_budget

    _, user = general_round_prompts(
        candidate, job, company, gap, LanguageMode(primary="en", mixed=False),
        {"apis": 1}, {"apis": 1}, followup_budget("moderate"),
    )
    assert "Checkout Cache" in user
    assert "CANDIDATE PROJECTS" in user


def test_coding_round_prompts_include_candidate_projects() -> None:
    p = Project(name="Checkout Cache", description="Cut p99 latency.", tech=["Redis"])
    candidate = _candidate([p])
    job = _job()
    gap = GapAnalysis(strengths=[], gaps=[], probe_targets=[], matched_skills=[], missing_skills=[], summary="")

    _, user = coding_round_prompts(
        candidate, job, gap, LanguageMode(primary="en", mixed=False),
        "caching", {"description": "d", "hint": "h"}, 3, 1,
    )
    assert "Checkout Cache" in user
    assert "CANDIDATE PROJECTS" in user


def test_behavioral_round_prompts_still_include_candidate_projects() -> None:
    """Regression guard: behavioral already used projects before this change
    (via inline formatting); confirm the switch to the shared helper didn't
    drop that."""
    p = Project(name="Checkout Cache", description="Cut p99 latency.", tech=["Redis"])
    candidate = _candidate([p])
    job = _job()
    gap = GapAnalysis(strengths=[], gaps=[], probe_targets=[], matched_skills=[], missing_skills=[], summary="")

    _, user = behavioral_round_prompts(
        candidate, job, gap, LanguageMode(primary="en", mixed=False), 2, 1,
    )
    assert "Checkout Cache" in user
    assert "[Redis]" in user  # tech now included, which the old inline version dropped
