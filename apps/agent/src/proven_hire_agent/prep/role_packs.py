"""Role-pack registry: pure, LLM-free selection logic for round-separated
question generation (see docs/QUESTION_GENERATION_SPEC.md).

Every function here is a deterministic function of its inputs — no network,
no LLM, no randomness — so the round-specific prep prompts are built from a
reproducible plan (role family, seniority band, competency weights, coding
topic/difficulty) *before* any model call, and the model's output is pinned
back to that plan afterward (same pattern as the existing ``language_mode``
pin in ``nodes.py``). This is what keeps selection testable independent of
which LLM adapter is wired — including the offline ``MockLLM``, which
ignores prompt content entirely.

Extensibility: ``IMPLEMENTED_FAMILIES`` ("data", "frontend", "backend") each
carry a full competency weight table (by seniority band) and a coding-topic
taxonomy, both grounded in real interview rubrics. ``STUB_FAMILIES`` ("ml",
"devops", "pm") are wired end to end — role inference recognizes them, every
lookup has a fallback, prep never crashes on them — but only carry a single
generic placeholder pack (marked ``STUB`` below), so their questions won't
have the same depth until a dedicated pack is written for each.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..shared_models import CandidateProfile, JobSpec


def _has_keyword(haystack: str, keyword: str) -> bool:
    """Word-boundary-ish match: a plain ``keyword in haystack`` substring
    check would false-positive short keywords like "go" inside unrelated
    words ("Django", "MongoDB"). This requires the keyword not be immediately
    preceded/followed by another alphanumeric character, while still
    tolerating multi-word/punctuated keywords like "ci/cd" or "site
    reliability" (the boundary check only looks at the ends of the phrase).
    """
    pattern = r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


RoleFamily = str
SeniorityBand = str  # "junior_mid" | "senior_staff"

IMPLEMENTED_FAMILIES: tuple[RoleFamily, ...] = ("data", "frontend", "backend")
STUB_FAMILIES: tuple[RoleFamily, ...] = ("ml", "devops", "pm")
ALL_FAMILIES: tuple[RoleFamily, ...] = IMPLEMENTED_FAMILIES + STUB_FAMILIES

# Backend's own vocabulary ("API", "database", "microservice") is too generic
# to be a positive signal — almost every JD mentions at least one — so it is
# the fallback when no other family's keywords hit, not a keyword list of
# its own.
DEFAULT_FAMILY: RoleFamily = "backend"


# --- seniority ---------------------------------------------------------------

SENIORITY_BAND: dict[str, SeniorityBand] = {
    "intern": "junior_mid",
    "junior": "junior_mid",
    "mid": "junior_mid",
    "senior": "senior_staff",
    "staff": "senior_staff",
    "principal": "senior_staff",
}

# Shared rising-difficulty band: the coding round pins to this directly; the
# general/behavioral prompts are told this number as a target ceiling.
DIFFICULTY_BY_SENIORITY: dict[str, int] = {
    "intern": 1,
    "junior": 2,
    "mid": 3,
    "senior": 4,
    "staff": 4,
    "principal": 5,
}

_SENIORITY_ORDER = ["intern", "junior", "mid", "senior", "staff", "principal"]

# (years_experience upper bound, exclusive) -> base band. First match wins;
# 12+ years falls through to "principal".
_YOE_BASE_BAND: list[tuple[int, str]] = [
    (1, "intern"),
    (3, "junior"),
    (5, "mid"),
    (8, "senior"),
    (12, "staff"),
]
_YOE_BASE_BAND_FALLBACK = "principal"

# Word-boundary-matched phrases that indicate demonstrated scope beyond raw
# tenure ("led", not "traveled"/"scaled" — see _has_keyword). Deliberately
# NEVER matched against candidate.headline/title — only against free-text
# achievements/summary — so a title alone (however inflated) cannot move the
# band; only described scope of work can.
_SCOPE_NUDGE_PHRASES: tuple[str, ...] = (
    "led",
    "owned end-to-end",
    "owned the",
    "architected",
    "founding engineer",
    "org-wide",
    "cross-team",
    "mentored",
)


def infer_seniority(candidate: CandidateProfile) -> str:
    """Deterministic seniority band from years_experience + demonstrated
    scope. NEVER reads ``candidate.headline`` — this is the calibration that
    keeps an inflated title ("Senior Software Engineer" at 1.5 YOE) from
    outranking its evidence: the title is never consulted, only years of
    experience and what the candidate says they actually did.
    """
    years = candidate.years_experience
    base = _YOE_BASE_BAND_FALLBACK
    for max_years_exclusive, band in _YOE_BASE_BAND:
        if years < max_years_exclusive:
            base = band
            break

    haystack = " ".join([candidate.summary_120w, *candidate.achievements]).lower()
    if any(_has_keyword(haystack, phrase) for phrase in _SCOPE_NUDGE_PHRASES):
        idx = min(_SENIORITY_ORDER.index(base) + 1, len(_SENIORITY_ORDER) - 1)
        return _SENIORITY_ORDER[idx]
    return base


def seniority_band(seniority: str) -> SeniorityBand:
    """Collapse the 6-value Seniority enum to the 2 question-shape bands."""
    return SENIORITY_BAND.get(seniority, "senior_staff")


# --- role family ---------------------------------------------------------------

# Checked in this fixed order against the JD text. First family with a
# keyword hit wins; falls through to DEFAULT_FAMILY if nothing matches.
_ROLE_FAMILY_CHECK_ORDER: tuple[RoleFamily, ...] = ("data", "frontend", "ml", "devops", "pm")

_ROLE_FAMILY_KEYWORDS: dict[RoleFamily, tuple[str, ...]] = {
    "data": (
        "data engineer",
        "etl",
        "elt",
        "data pipeline",
        "airflow",
        "spark",
        "databricks",
        "data warehouse",
        "bigquery",
        "snowflake",
        "redshift",
        "lakehouse",
        "dbt",
        "data platform",
    ),
    "frontend": (
        "frontend",
        "front-end",
        "front end",
        "react",
        "vue",
        "angular",
        "svelte",
        "ui engineer",
        "web developer",
        "javascript developer",
        "css",
        "dom",
        "browser",
    ),
    # STUB families: recognized so infer_role_family routes to them, but each
    # only backs onto a single generic placeholder pack (see STUB_WEIGHTS /
    # _STUB_TOPICS below) — not a researched taxonomy.
    "ml": (
        "machine learning",
        "ml engineer",
        "deep learning",
        "pytorch",
        "tensorflow",
        "nlp",
        "computer vision",
        "mlops",
    ),
    "devops": (
        "devops",
        "site reliability",
        "sre",
        "infrastructure engineer",
        "terraform",
        "platform engineer",
        "kubernetes administrator",
    ),
    "pm": (
        "product manager",
        "product management",
        "product owner",
        "roadmap",
        "stakeholder management",
    ),
}


def infer_role_family(job: JobSpec) -> RoleFamily:
    """Ordered keyword match over title + tech_stack + must_have +
    responsibilities + raw_text (lowercased, joined). See
    ``_ROLE_FAMILY_CHECK_ORDER`` for the tie-break priority.
    """
    haystack = " ".join(
        [job.title, *job.tech_stack, *job.must_have, *job.responsibilities, job.raw_text]
    ).lower()
    for family in _ROLE_FAMILY_CHECK_ORDER:
        if any(_has_keyword(haystack, kw) for kw in _ROLE_FAMILY_KEYWORDS[family]):
            return family
    return DEFAULT_FAMILY


def is_stub_family(role_family: RoleFamily) -> bool:
    return role_family in STUB_FAMILIES


# --- competency weight tables --------------------------------------------------

# (role_family, seniority_band) -> {competency_key: weight}. Weights sum to
# ~1.0 within each table. The two "data" tables and the "backend"
# senior_staff table are the reference docs' own weight tables, verbatim
# (see docs/QUESTION_GENERATION_SPEC.md worked example / the interview
# design notes this was built from). The "backend" junior_mid and both
# "frontend" tables are this implementation's own construction (no full
# junior-backend or frontend reference doc was supplied) — flagged for
# review below.
COMPETENCY_WEIGHTS: dict[tuple[RoleFamily, SeniorityBand], dict[str, float]] = {
    ("data", "junior_mid"): {
        "sql": 0.30,
        "python": 0.20,
        "etl_pipelines": 0.20,
        "spark_lakehouse": 0.10,
        "databricks_delta": 0.08,
        "debugging_data_quality": 0.07,
        "dsa_fundamentals": 0.05,
    },
    ("data", "senior_staff"): {
        "etl_pipeline_architecture": 0.25,
        "sql": 0.20,
        "python": 0.15,
        "spark_distributed": 0.15,
        "data_quality_governance": 0.10,
        "debugging_incident_response": 0.10,
        "stakeholder_leadership": 0.05,
    },
    ("backend", "senior_staff"): {
        "system_design": 0.25,
        "performance_optimization": 0.20,
        "concurrency_distributed_systems": 0.15,
        "databases_caching": 0.15,
        "production_reliability_debugging": 0.10,
        "coding_dsa_fundamentals": 0.07,
        "cicd_engineering_excellence": 0.03,
        "staff_leadership_communication": 0.05,
    },
    # Own construction (no junior/mid backend reference doc supplied).
    ("backend", "junior_mid"): {
        "implementation_coding": 0.25,
        "dsa_fundamentals": 0.20,
        "databases_sql": 0.20,
        "debugging": 0.15,
        "system_design_basics": 0.10,
        "communication": 0.10,
    },
    # Own construction — the frontend reference doc was not supplied in
    # full, only two illustrative phrases (an API-works-in-Postman-but-not-
    # the-browser debugging scenario, and a 10k-row dashboard with Redux/
    # rendering red flags). Please review/replace these weights directly.
    ("frontend", "junior_mid"): {
        "react_component_design": 0.30,
        "javascript_typescript": 0.20,
        "state_management": 0.15,
        "rendering_performance": 0.10,
        "api_networking_debugging": 0.15,
        "css_accessibility": 0.05,
        "dsa_fundamentals": 0.05,
    },
    ("frontend", "senior_staff"): {
        "frontend_architecture": 0.25,
        "rendering_performance_at_scale": 0.20,
        "state_management_at_scale": 0.15,
        "api_networking_debugging": 0.15,
        "design_systems_cross_team": 0.10,
        "leadership_mentorship": 0.10,
        "dsa_fundamentals": 0.05,
    },
}

# STUB — placeholder only, not a researched table. TODO(role-packs): each
# stub family needs its own reference-doc-quality weight table + coding
# taxonomy before it should be considered "implemented" rather than "doesn't
# crash but is generic."
STUB_WEIGHTS: dict[RoleFamily, dict[str, float]] = {
    "ml": {"applied_ml_reasoning": 0.5, "system_design": 0.3, "coding": 0.2},
    "devops": {"reliability_operations": 0.5, "system_design": 0.3, "coding": 0.2},
    "pm": {"product_reasoning": 0.5, "communication": 0.3, "technical_fluency": 0.2},
}

# Competencies whose relative weight should soften when the JD only lists
# the underlying skill as nice_to_have (not must_have) — the "JD-honest
# leniency" rule (e.g. "don't reject on weak Databricks, the JD says they'll
# learn it"), driven off JobSpec's existing structured must_have/
# nice_to_have fields rather than a fragile raw-text regex.
_OPTIONAL_COMPETENCY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "spark_lakehouse": ("spark",),
    "spark_distributed": ("spark",),
    "databricks_delta": ("databricks", "delta"),
    "dsa_fundamentals": ("dsa", "leetcode", "data structures"),
    "coding_dsa_fundamentals": ("dsa", "leetcode", "data structures"),
    "cicd_engineering_excellence": ("ci/cd", "cicd", "ci-cd", "continuous integration"),
    "css_accessibility": ("accessibility", "a11y"),
    "design_systems_cross_team": ("design system",),
}


def infer_competency_weights(
    role_family: RoleFamily, band: SeniorityBand, job: JobSpec
) -> dict[str, float]:
    """The family+band's base weight table, softened by the JD-honesty nudge:
    any competency whose only keyword hit is in ``job.nice_to_have`` (not
    ``job.must_have``) has its weight halved, and the freed weight is
    redistributed proportionally across the rest. Pure and deterministic —
    same (role_family, band, job) always yields the same weights.
    """
    base = COMPETENCY_WEIGHTS.get((role_family, band))
    if base is None:
        base = STUB_WEIGHTS.get(role_family, COMPETENCY_WEIGHTS[(DEFAULT_FAMILY, band)])
    weights = dict(base)

    must_have_text = " ".join(job.must_have).lower()
    nice_to_have_text = " ".join(job.nice_to_have).lower()

    freed = 0.0
    nudged: set[str] = set()
    for comp, keywords in _OPTIONAL_COMPETENCY_KEYWORDS.items():
        if comp not in weights:
            continue
        in_must_have = any(_has_keyword(must_have_text, kw) for kw in keywords)
        in_nice_to_have = any(_has_keyword(nice_to_have_text, kw) for kw in keywords)
        if in_nice_to_have and not in_must_have:
            half = weights[comp] / 2.0
            freed += weights[comp] - half
            weights[comp] = half
            nudged.add(comp)

    if freed > 0:
        redistributable = {k: v for k, v in weights.items() if k not in nudged}
        total = sum(redistributable.values())
        if total > 0:
            for comp, value in redistributable.items():
                weights[comp] += freed * (value / total)

    return weights


def allocate_question_counts(weights: dict[str, float], total: int) -> dict[str, int]:
    """Largest-remainder (Hamilton) apportionment: turns a weight table + a
    target question count into exact per-competency integer counts summing
    to ``total``. Pure and deterministic; ties in the remainder step break by
    the insertion order of ``weights`` (dicts are order-preserving, and
    ``sorted`` is stable).
    """
    if total <= 0 or not weights:
        return dict.fromkeys(weights, 0)
    weight_sum = sum(weights.values())
    if weight_sum <= 0:
        return dict.fromkeys(weights, 0)

    raw = {k: (w / weight_sum) * total for k, w in weights.items()}
    floors = {k: int(v) for k, v in raw.items()}
    remainder = total - sum(floors.values())

    order = sorted(weights.keys(), key=lambda k: raw[k] - floors[k], reverse=True)
    counts = dict(floors)
    for k in order[:remainder]:
        counts[k] += 1
    return counts


# --- coding round: topic + difficulty taxonomy ---------------------------------

# Each family's topics dict is checked in insertion order (= priority); the
# first topic whose keywords hit `tech_stack` wins. A topic with an empty
# keyword tuple is never matched by the loop — it is reached only via the
# explicit `default_topic` fallback when nothing else matched.

_BACKEND_TOPICS: dict[str, dict] = {
    "concurrency": {
        "keywords": ("go", "golang", "rust", "java", "kotlin", "erlang", "elixir"),
        "description": "reasoning about a race condition or concurrent-update scenario",
        "hint": "Think about what happens if two requests read the same value before either writes back.",
    },
    "caching": {
        "keywords": ("redis", "memcached", "cdn"),
        "description": "cache-aside vs. cache-stampede reasoning",
        "hint": "What happens to the database if the cached key expires under heavy concurrent load?",
    },
    "datastore_indexing": {
        "keywords": ("postgres", "postgresql", "mysql", "dynamodb"),
        "description": "index/query-plan reasoning, or a small production-flavored data-structure design",
        "hint": "Which single column, if indexed, would eliminate the full scan here?",
    },
    "systems_design_micro": {
        "keywords": (),
        "description": "a scoped mini-system (rate limiter, LRU cache, top-K) reasoned aloud",
        "hint": "Start by naming the one operation that must be O(1) or near it, then work backward.",
    },
}
_BACKEND_DEFAULT_TOPIC = "systems_design_micro"

_FRONTEND_TOPICS: dict[str, dict] = {
    "rendering_performance": {
        "keywords": ("react", "vue", "angular", "svelte"),
        "description": "a large-list rendering-performance scenario (virtualization/memoization), not Big-O trivia",
        "hint": "What would change if only the visible rows were mounted to the DOM?",
    },
    "state_management": {
        "keywords": ("redux", "zustand", "mobx", "recoil"),
        "description": "where a piece of state should live, and what breaks if it's all put in one global store",
        "hint": "Does any component outside this subtree actually need this value?",
    },
    "async_data_debugging": {
        "keywords": ("fetch", "axios", "graphql", "rest api"),
        "description": "a request that works in an API client but fails in the browser (CORS/credentials/headers)",
        "hint": "Compare exactly what the API client sends vs. what the browser sends automatically.",
    },
    "browser_runtime_fundamentals": {
        "keywords": (),
        "description": "event loop / reflow-repaint / debounce-throttle reasoning tied to a concrete UI bug",
        "hint": "Is this fighting a synchronous blocking call, or the browser's paint cycle?",
    },
}
_FRONTEND_DEFAULT_TOPIC = "browser_runtime_fundamentals"

_DATA_TOPICS: dict[str, dict] = {
    "sql_under_pressure": {
        "keywords": ("sql", "postgres", "postgresql", "mysql", "bigquery", "snowflake", "redshift"),
        "description": "a window-function / dedup / top-N-per-group query on a company-specific table",
        "hint": "Partition by the grouping column and order by the metric before filtering the rank.",
    },
    "python_data_wrangling": {
        "keywords": ("pandas",),
        "description": "a large-file-under-memory-constraint or dedup-keep-latest data-wrangling problem",
        "hint": "Can you avoid holding the whole dataset in memory — what would you stream instead?",
    },
    "pipeline_debugging": {
        "keywords": ("airflow", "dagster", "spark", "kafka", "airbyte"),
        "description": "a row-count-discrepancy / late-arriving-data / duplicate-event dedup scenario",
        "hint": "Walk the pipeline stage by stage — where does the row count actually change?",
    },
}
_DATA_DEFAULT_TOPIC = "sql_under_pressure"

# STUB — generic fallback, not a researched taxonomy.
_STUB_TOPIC = "system_reasoning"
_STUB_TOPICS: dict[str, dict] = {
    _STUB_TOPIC: {
        "keywords": (),
        "description": (
            "a scoped, spoken scenario question grounded in the JD's own "
            "tech_stack/responsibilities (STUB pack — not family-specific)"
        ),
        "hint": "Start from the JD's stated responsibilities and reason about the first thing that would break at scale.",
    }
}

_CODING_TAXONOMY: dict[RoleFamily, tuple[dict[str, dict], str]] = {
    "backend": (_BACKEND_TOPICS, _BACKEND_DEFAULT_TOPIC),
    "frontend": (_FRONTEND_TOPICS, _FRONTEND_DEFAULT_TOPIC),
    "data": (_DATA_TOPICS, _DATA_DEFAULT_TOPIC),
    "ml": (_STUB_TOPICS, _STUB_TOPIC),
    "devops": (_STUB_TOPICS, _STUB_TOPIC),
    "pm": (_STUB_TOPICS, _STUB_TOPIC),
}


def select_coding_topic(role_family: RoleFamily, tech_stack: list[str], seniority: str) -> tuple[str, int]:
    """Deterministic (topic, difficulty) pick, keyed off role family +
    the candidate's actual stack + inferred seniority — nothing else.
    difficulty = DIFFICULTY_BY_SENIORITY[seniority]. topic = the first
    taxonomy entry (in priority/insertion order) whose keywords appear in
    tech_stack; falls through to the family's documented default topic
    otherwise. An unrecognized role_family falls back to DEFAULT_FAMILY's
    taxonomy (defensive; infer_role_family always returns a known family).
    """
    difficulty = DIFFICULTY_BY_SENIORITY.get(seniority, 3)
    topics, default_topic = _CODING_TAXONOMY.get(role_family, _CODING_TAXONOMY[DEFAULT_FAMILY])
    haystack = " ".join(tech_stack).lower()
    for topic, meta in topics.items():
        if meta["keywords"] and any(_has_keyword(haystack, kw) for kw in meta["keywords"]):
            return topic, difficulty
    return default_topic, difficulty


def coding_topic_meta(role_family: RoleFamily, topic: str) -> dict:
    """The taxonomy entry (description/hint/keywords) for `topic` within
    `role_family`'s taxonomy — used to build the coding-round prompt and as
    the canned fallback hint if the model returns an empty followups list.
    """
    topics, default_topic = _CODING_TAXONOMY.get(role_family, _CODING_TAXONOMY[DEFAULT_FAMILY])
    return topics.get(topic, topics[default_topic])
