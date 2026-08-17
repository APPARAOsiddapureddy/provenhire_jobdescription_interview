"""Offline, LLM-free tests for the round-separated question-generation role
packs (``prep/role_packs.py``): role-family inference, seniority calibration,
JD-driven competency weighting, coding topic/difficulty selection, and
question-count apportionment. Every function under test is pure (no network,
no LLM, no randomness), so these assert *determinism* directly: same inputs
must always produce the exact same output.
"""

from __future__ import annotations

import pytest

from proven_hire_agent.prep import role_packs
from proven_hire_agent.shared_models import CandidateProfile, JobSpec


def _candidate(
    years_experience: int = 3,
    headline: str = "Software Engineer",
    achievements: list[str] | None = None,
    summary_120w: str = "",
) -> CandidateProfile:
    return CandidateProfile(
        name="Test Candidate",
        headline=headline,
        summary_120w=summary_120w,
        years_experience=years_experience,
        seniority="mid",
        skills=["Python"],
        projects=[],
        achievements=achievements or [],
        education=[],
        spoken_languages=["English"],
    )


def _job(
    title: str = "Software Engineer",
    tech_stack: list[str] | None = None,
    must_have: list[str] | None = None,
    nice_to_have: list[str] | None = None,
    responsibilities: list[str] | None = None,
    raw_text: str = "",
) -> JobSpec:
    return JobSpec(
        title=title,
        company_name="Acme",
        seniority="mid",
        must_have=must_have or [],
        nice_to_have=nice_to_have or [],
        responsibilities=responsibilities or [],
        tech_stack=tech_stack or [],
        raw_text=raw_text,
    )


# --- seniority -----------------------------------------------------------------


def test_infer_seniority_is_deterministic() -> None:
    candidate = _candidate(years_experience=4, achievements=["Shipped a service"])
    assert role_packs.infer_seniority(candidate) == role_packs.infer_seniority(candidate)


def test_infer_seniority_ignores_inflated_title() -> None:
    """A candidate titled 'Senior Software Engineer' with 1 YOE and no
    demonstrated scope must still band as junior — the title is never read."""
    candidate = _candidate(
        years_experience=1,
        headline="Senior Software Engineer",
        summary_120w="Wrote SQL dashboards for the growth team.",
        achievements=["Built a small ETL script"],
    )
    assert role_packs.infer_seniority(candidate) == "junior"


def test_infer_seniority_yoe_bands_without_scope_evidence() -> None:
    cases = [
        (0, "intern"),
        (2, "junior"),
        (4, "mid"),
        (6, "senior"),
        (10, "staff"),
        (15, "principal"),
    ]
    for years, expected in cases:
        candidate = _candidate(years_experience=years)
        assert role_packs.infer_seniority(candidate) == expected, years


def test_infer_seniority_scope_nudge_bumps_one_band() -> None:
    """Demonstrated scope in achievements/summary bumps the YOE-derived band
    by one, even though the title says nothing more senior."""
    base = _candidate(years_experience=6)  # -> "senior" with no scope evidence
    assert role_packs.infer_seniority(base) == "senior"

    scoped = _candidate(
        years_experience=6,
        achievements=["Led the redesign of the ledger service"],
    )
    assert role_packs.infer_seniority(scoped) == "staff"


def test_infer_seniority_scope_nudge_caps_at_principal() -> None:
    candidate = _candidate(
        years_experience=15,  # already "principal" with no scope evidence
        achievements=["Founding engineer, architected the platform"],
    )
    assert role_packs.infer_seniority(candidate) == "principal"


@pytest.mark.parametrize("seniority", ["intern", "junior", "mid", "senior", "staff", "principal"])
def test_difficulty_by_seniority_covers_every_band(seniority: str) -> None:
    assert 1 <= role_packs.DIFFICULTY_BY_SENIORITY[seniority] <= 5


def test_seniority_band_collapses_to_two_values() -> None:
    for s in ["intern", "junior", "mid"]:
        assert role_packs.seniority_band(s) == "junior_mid"
    for s in ["senior", "staff", "principal"]:
        assert role_packs.seniority_band(s) == "senior_staff"


# --- role family -----------------------------------------------------------------


def test_infer_role_family_data() -> None:
    job = _job(
        title="Data Engineer",
        tech_stack=["Airflow", "dbt", "Spark", "BigQuery"],
        responsibilities=["Build ETL pipelines"],
    )
    assert role_packs.infer_role_family(job) == "data"


def test_infer_role_family_frontend() -> None:
    job = _job(title="Frontend Engineer", tech_stack=["React", "Redux", "TypeScript"])
    assert role_packs.infer_role_family(job) == "frontend"


def test_infer_role_family_falls_back_to_backend() -> None:
    """Generic backend vocabulary ('API', 'microservices') is too common to
    be a positive signal — backend is the fallback, not a keyword hit."""
    job = _job(title="Software Engineer", tech_stack=["API", "microservices", "PostgreSQL"])
    assert role_packs.infer_role_family(job) == "backend"


def test_infer_role_family_stub_families() -> None:
    ml_job = _job(title="Machine Learning Engineer", tech_stack=["PyTorch", "TensorFlow"])
    devops_job = _job(title="DevOps Engineer", tech_stack=["Terraform", "Kubernetes"], raw_text="site reliability")
    pm_job = _job(title="Product Manager", raw_text="own the roadmap and stakeholder management")

    assert role_packs.infer_role_family(ml_job) == "ml"
    assert role_packs.infer_role_family(devops_job) == "devops"
    assert role_packs.infer_role_family(pm_job) == "pm"


def test_infer_role_family_is_deterministic() -> None:
    job = _job(title="Data Engineer", tech_stack=["Airflow", "React"])
    assert role_packs.infer_role_family(job) == role_packs.infer_role_family(job)


# --- competency weights + apportionment -------------------------------------------


def test_infer_competency_weights_sums_to_one() -> None:
    for family, band in role_packs.COMPETENCY_WEIGHTS:
        weights = role_packs.infer_competency_weights(family, band, _job())
        assert abs(sum(weights.values()) - 1.0) < 1e-9, (family, band)


def test_infer_competency_weights_nice_to_have_nudge() -> None:
    """A skill listed only under nice_to_have gets a strictly lower weight
    than the same skill listed under must_have (the 'JD-honest leniency' rule)."""
    lenient_job = _job(must_have=["SQL", "Python"], nice_to_have=["Spark"])
    strict_job = _job(must_have=["SQL", "Python", "Spark"], nice_to_have=[])

    lenient = role_packs.infer_competency_weights("data", "junior_mid", lenient_job)
    strict = role_packs.infer_competency_weights("data", "junior_mid", strict_job)

    assert lenient["spark_lakehouse"] < strict["spark_lakehouse"]


def test_infer_competency_weights_is_deterministic() -> None:
    job = _job(must_have=["SQL"], nice_to_have=["Spark", "Databricks"])
    a = role_packs.infer_competency_weights("data", "junior_mid", job)
    b = role_packs.infer_competency_weights("data", "junior_mid", job)
    assert a == b


@pytest.mark.parametrize("total", [1, 5, 6, 7, 12, 13])
def test_allocate_question_counts_sums_to_total(total: int) -> None:
    weights = role_packs.COMPETENCY_WEIGHTS[("data", "junior_mid")]
    counts = role_packs.allocate_question_counts(weights, total)
    assert sum(counts.values()) == total


def test_allocate_question_counts_zero_weight_never_gets_a_stray_question() -> None:
    weights = {"a": 0.5, "b": 0.5, "c": 0.0}
    counts = role_packs.allocate_question_counts(weights, 4)
    assert counts["c"] == 0
    assert sum(counts.values()) == 4


def test_allocate_question_counts_is_deterministic() -> None:
    weights = role_packs.COMPETENCY_WEIGHTS[("backend", "senior_staff")]
    a = role_packs.allocate_question_counts(weights, 6)
    b = role_packs.allocate_question_counts(weights, 6)
    assert a == b


def test_allocate_question_counts_handles_empty_and_zero_total() -> None:
    assert role_packs.allocate_question_counts({}, 5) == {}
    assert role_packs.allocate_question_counts({"a": 1.0}, 0) == {"a": 0}


# --- coding topic + difficulty selection -------------------------------------------


@pytest.mark.parametrize(
    ("tech_stack", "expected_topic"),
    [
        (["Go", "PostgreSQL"], "concurrency"),  # first match wins: concurrency before datastore_indexing
        (["Redis"], "caching"),
        (["PostgreSQL"], "datastore_indexing"),
        (["Excel"], "systems_design_micro"),  # no keyword match -> default
    ],
)
def test_select_coding_topic_backend(tech_stack: list[str], expected_topic: str) -> None:
    topic, _difficulty = role_packs.select_coding_topic("backend", tech_stack, "mid")
    assert topic == expected_topic


@pytest.mark.parametrize(
    ("tech_stack", "expected_topic"),
    [
        (["React", "Redux"], "rendering_performance"),  # first match wins
        (["Redux"], "state_management"),
        (["Fetch", "REST API"], "async_data_debugging"),
        (["jQuery"], "browser_runtime_fundamentals"),  # no keyword match -> default
    ],
)
def test_select_coding_topic_frontend(tech_stack: list[str], expected_topic: str) -> None:
    topic, _difficulty = role_packs.select_coding_topic("frontend", tech_stack, "mid")
    assert topic == expected_topic


@pytest.mark.parametrize(
    ("tech_stack", "expected_topic"),
    [
        (["PostgreSQL", "Pandas"], "sql_under_pressure"),  # first match wins
        (["Pandas"], "python_data_wrangling"),
        (["Airflow", "Kafka"], "pipeline_debugging"),
        (["Excel"], "sql_under_pressure"),  # no keyword match -> default (also sql_under_pressure)
    ],
)
def test_select_coding_topic_data(tech_stack: list[str], expected_topic: str) -> None:
    topic, _difficulty = role_packs.select_coding_topic("data", tech_stack, "mid")
    assert topic == expected_topic


def test_select_coding_topic_difficulty_matches_seniority_map() -> None:
    for seniority, expected_difficulty in role_packs.DIFFICULTY_BY_SENIORITY.items():
        _topic, difficulty = role_packs.select_coding_topic("backend", ["Go"], seniority)
        assert difficulty == expected_difficulty


def test_select_coding_topic_is_deterministic() -> None:
    a = role_packs.select_coding_topic("data", ["Airflow", "Spark", "SQL"], "senior")
    b = role_packs.select_coding_topic("data", ["Airflow", "Spark", "SQL"], "senior")
    assert a == b


def test_coding_topic_meta_has_hint_and_description() -> None:
    topic, _difficulty = role_packs.select_coding_topic("data", ["Airflow"], "mid")
    meta = role_packs.coding_topic_meta("data", topic)
    assert meta["hint"]
    assert meta["description"]


# --- stub families never crash -------------------------------------------------


@pytest.mark.parametrize("family", ["ml", "devops", "pm"])
def test_stub_families_are_wired_without_crashing(family: str) -> None:
    assert role_packs.is_stub_family(family)

    weights = role_packs.infer_competency_weights(family, "junior_mid", _job())
    assert weights
    assert abs(sum(weights.values()) - 1.0) < 1e-9

    counts = role_packs.allocate_question_counts(weights, 5)
    assert sum(counts.values()) == 5

    topic, difficulty = role_packs.select_coding_topic(family, ["Python"], "mid")
    assert topic == "system_reasoning"
    assert 1 <= difficulty <= 5

    meta = role_packs.coding_topic_meta(family, topic)
    assert meta["hint"]


def test_implemented_families_are_not_stubs() -> None:
    for family in role_packs.IMPLEMENTED_FAMILIES:
        assert not role_packs.is_stub_family(family)
