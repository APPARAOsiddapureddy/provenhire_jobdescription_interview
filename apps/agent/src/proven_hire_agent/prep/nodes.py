"""Async node functions for the WP-6 prep graph.

Every node takes ``(state, deps)`` and returns a partial ``PrepState`` dict with
only the key(s) it computes; LangGraph merges those into the running state. Deps
are injected into the graph via :func:`functools.partial` (see ``graph.py``), so
each compiled node presents the ``(state)`` signature LangGraph expects.

``fetch_cv`` is deliberately best-effort and offline-tolerant: it delegates to
:func:`proven_hire_agent.prep.cv_extract.extract_cv_text`, which parses an
uploaded PDF/DOCX (``data:`` URL or fetched ``http(s)`` URL) into real text and,
on any failure, falls back to the URL string itself as the document text. This
keeps the whole pipeline — and the existing ``POST /api/prep`` test that points
at an unreachable example.com — green without network access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..core.adapters.mock import build_mock
from ..core.logging import get_logger
from ..shared_models import (
    CandidateProfile,
    Citation,
    CompanyIntel,
    GapAnalysis,
    JobSpec,
    PlannedQuestion,
    QuestionPlan,
)
from . import role_packs
from .cv_extract import extract_cv_text
from .follow_up_depth import FollowUpBudget, followup_budget
from .prompts import (
    behavioral_round_prompts,
    coding_round_prompts,
    company_research_prompts,
    cv_analysis_prompts,
    gap_matching_prompts,
    general_round_prompts,
    jd_analysis_prompts,
    language_name,
)
from .state import PrepState

if TYPE_CHECKING:
    from ..core.deps import Deps

log = get_logger(__name__)


async def _mark(state: PrepState, deps: Deps, step: str) -> None:
    """Record that ``step`` finished, if running against a known session.

    Best-effort: a missing/closed session must never crash prep — the progress
    signal is for the UI only.
    """
    session_id = state.get("session_id")
    if not session_id:
        return
    try:
        await deps.repo.mark_progress(session_id, step)
    except Exception as exc:  # noqa: BLE001 - progress is advisory only
        log.warning("mark_progress(%s) failed (%s)", step, exc)


async def _warn(state: PrepState, deps: Deps, warnings: list[str]) -> None:
    """Attach input-quality/fallback warnings to the session (best-effort)."""
    session_id = state.get("session_id")
    if not session_id or not warnings:
        return
    try:
        await deps.repo.add_warnings(session_id, warnings)
    except Exception as exc:  # noqa: BLE001 - warnings are advisory only
        log.warning("add_warnings failed (%s)", exc)


async def fetch_cv(state: PrepState, deps: Deps) -> PrepState:
    """Best-effort parse of the CV document into text; fall back to the URL string.

    Delegates to :func:`extract_cv_text`, which converts an uploaded PDF/DOCX
    (``data:`` URL or fetched ``http(s)`` URL) into plain text via markitdown
    (Gemini fallback for scanned/image PDFs). Any returned warnings are attached
    to the session via ``_warn``.

    Idempotent: if ``cv_text`` was already resolved (the caller pre-fetched it so
    it could validate inputs), this is a no-op — we never parse the CV twice. We
    check key *presence*, not truthiness: an unreadable document resolves to ``""``
    and must NOT trigger a re-parse (which would re-warn and re-bill Gemini).
    """
    if "cv_text" in state:
        return {}
    req = state["req"]
    try:
        cv_text, warnings = await extract_cv_text(req.cv_url, deps)
    except Exception as exc:  # noqa: BLE001 - best-effort: any failure -> raw fallback
        log.warning("fetch_cv: extraction failed, using cv_url as text (%s)", exc)
        return {"cv_text": req.cv_url}
    if warnings:
        await _warn(state, deps, warnings)
    return {"cv_text": cv_text}


async def cv_analysis(state: PrepState, deps: Deps) -> PrepState:
    """Extract a ``CandidateProfile`` from the fetched CV text."""
    system, user = cv_analysis_prompts(state["cv_text"])
    try:
        candidate = await deps.llm.complete_json(
            system=system, user=user, schema=CandidateProfile
        )
    except Exception as exc:  # noqa: BLE001 - resilient: degrade, don't crash prep
        log.warning("cv_analysis failed, using minimal profile (%s)", exc)
        candidate = build_mock(CandidateProfile)
        await _warn(state, deps, ["Could not analyze the CV; used a minimal profile."])
    await _mark(state, deps, "cv_analysis")
    return {"candidate": candidate}


async def jd_analysis(state: PrepState, deps: Deps) -> PrepState:
    """Extract a ``JobSpec`` from the job description text."""
    req = state["req"]
    system, user = jd_analysis_prompts(req.jd_text, req.company)
    try:
        job = await deps.llm.complete_json(system=system, user=user, schema=JobSpec)
    except Exception as exc:  # noqa: BLE001 - resilient: degrade, don't crash prep
        log.warning("jd_analysis failed, using minimal job spec (%s)", exc)
        job = build_mock(JobSpec)
        await _warn(
            state, deps, ["Could not analyze the job description; used a minimal spec."]
        )
    await _mark(state, deps, "jd_analysis")
    return {"job": job}


def _empty_company_intel(name: str) -> CompanyIntel:
    """A valid, empty ``CompanyIntel`` for a junk/unknown company (no fabrication)."""
    return CompanyIntel(
        name=name or "Unknown",
        summary="",
        industry=None,
        tech_stack=[],
        values=[],
        interview_process=[],
        recent_news=[],
        citations=[],
    )


async def company_research(state: PrepState, deps: Deps) -> PrepState:
    """Search the web for company interview intel and synthesize ``CompanyIntel``.

    If the company name was flagged junk (``company_ok`` is False) this skips all
    search + LLM work and returns an empty-but-valid intel, so we never fabricate
    company knowledge from a meaningless name or waste calls on it.
    """
    req = state["req"]
    company = req.company

    if not state.get("company_ok", True):
        log.info("company_research: skipping for junk company %r", company)
        await _mark(state, deps, "company_research")
        return {"company": _empty_company_intel(company)}

    primary = req.language_mode.primary

    queries: list[tuple[str, str]] = [(f"{company} interview process", "en")]
    if primary != "en":
        localized = f"{company} interview process {language_name(primary)}"
        queries.append((localized, primary))

    results = []
    for query, lang in queries:
        try:
            results.extend(await deps.search.search(query, lang=lang, max_results=4))
        except Exception as exc:  # noqa: BLE001 - search is best-effort
            log.warning("company_research: search failed for %r (%s)", query, exc)

    snippets = "\n".join(f"- {r.title}: {r.snippet}" for r in results) or "(no results)"
    system, user = company_research_prompts(company, snippets)
    try:
        intel = await deps.llm.complete_json(
            system=system, user=user, schema=CompanyIntel
        )
    except Exception as exc:  # noqa: BLE001 - resilient: degrade, don't crash prep
        log.warning("company_research failed, using minimal intel (%s)", exc)
        intel = _empty_company_intel(company)
        await _warn(
            state, deps, ["Could not research the company; proceeding without intel."]
        )

    citations = [
        Citation(title=r.title, url=r.url, snippet=r.snippet) for r in results
    ]
    intel = intel.model_copy(update={"citations": citations})
    await _mark(state, deps, "company_research")
    return {"company": intel}


async def gap_matching(state: PrepState, deps: Deps) -> PrepState:
    """Compare candidate vs job into a ``GapAnalysis`` (join of cv + jd)."""
    system, user = gap_matching_prompts(state["candidate"], state["job"])
    try:
        gap = await deps.llm.complete_json(
            system=system, user=user, schema=GapAnalysis
        )
    except Exception as exc:  # noqa: BLE001 - resilient: degrade, don't crash prep
        log.warning("gap_matching failed, using minimal analysis (%s)", exc)
        gap = build_mock(GapAnalysis)
        await _warn(state, deps, ["Could not compute the gap analysis; used a minimal one."])
    await _mark(state, deps, "gap_matching")
    return {"gap": gap}


# Body sections a pack contributes to the planner prompt, and the total budget.
# Bounded so a growing library can never blow up prep prompt size (golden rule:
# keep prompts compact).
_HINT_SECTIONS = frozenset({"round structure", "question bank", "signals", "pitfalls"})
_HINT_CHAR_BUDGET = 1500


def _extract_hint_sections(body_md: str) -> str:
    """Pull the planner-relevant ``##`` sections out of a pack body, in order."""
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in body_md.splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            current = [] if title.lower() in _HINT_SECTIONS else None
            if current is not None:
                sections.append((title, current))
        elif current is not None:
            current.append(line)
    parts = [f"{title}:\n" + "\n".join(lines).strip() for title, lines in sections if lines]
    return "\n".join(p for p in parts if not p.endswith(":\n"))


def _skill_library_hint(
    company: str, role: str, level: str, skills_dir: str | None = None
) -> str:
    """Playbook context for the planner (WP-10 retrieval): provenance header
    plus the pack's question bank / signals / pitfalls, capped at
    ``_HINT_CHAR_BUDGET`` characters.

    Best-effort and additive: any failure (missing/un-parseable library, import
    error) returns an empty string so prep never depends on the skill store.
    """
    try:
        from ..skilllib import effective_confidence, find_relevant

        skills = find_relevant(skills_dir, company=company, role=role, level=level)
        if not skills:
            return ""
        blocks: list[str] = []
        for skill in skills:
            fm = skill.frontmatter
            header = (
                f"### {fm.company} · {fm.role} · {fm.level} "
                f"[{fm.status}; confidence {effective_confidence(fm):.2f} "
                f"from {fm.source_runs} run(s); verified {fm.last_verified}]"
            )
            extract = _extract_hint_sections(skill.body_md)
            blocks.append(f"{header}\n{extract}" if extract else header)
        hint = "\n\n".join(blocks)
        if len(hint) > _HINT_CHAR_BUDGET:
            hint = hint[:_HINT_CHAR_BUDGET].rsplit("\n", 1)[0] + "\n[truncated]"
        return hint
    except Exception:  # noqa: BLE001 - retrieval is strictly best-effort
        return ""


class _RoundQuestions(BaseModel):
    """LLM-call plumbing only: wraps a list so ``complete_json`` (which needs
    *a* Pydantic schema per call) can return several ``PlannedQuestion``s from
    one round call. Never exported, never added to ``packages/shared`` or the
    ``MODELS`` registry — it's unwrapped into ``PlannedQuestion``s before
    anything touches ``PrepState``.
    """

    questions: list[PlannedQuestion]


# Fixed per-round question counts: 1 intro + 8 weighted-technical + 1 wrap
# from the general round, 1 from coding, 4 from behavioral = 15 total (was
# 12). Raised alongside the shorter-question prompt rules below: real
# candidates hearing (not reading) a question can't hold a long compound ask
# in their head, so questions are now shorter/single-focus, which means more
# of them fit in the same real-world session time. Unreached tail questions
# are harmless (same as before) if the session hits its time limit first.
_GENERAL_TECHNICAL_COUNT = 8
_BEHAVIORAL_COUNT = 4
_TIME_BUDGET_MIN = 40
_SECTION_ORDER: tuple[str, ...] = ("intro", "behavioral", "technical", "coding", "wrap")

# Canned fallback follow-up when a behavioral question comes back with an
# empty followups list (e.g. the offline MockLLM) — guarantees followups[0]
# is always a real individual-contribution probe, never missing.
_BEHAVIORAL_FALLBACK_PROBE = "What did YOU specifically decide or do here — not what the team did?"


def _reid(questions: list[PlannedQuestion], prefix: str) -> list[PlannedQuestion]:
    """Re-assign ids as ``{prefix}-{n}`` so ids stay unique across rounds
    regardless of what the model (or the offline mock, which always answers
    "mock") returned.
    """
    return [q.model_copy(update={"id": f"{prefix}-{i + 1}"}) for i, q in enumerate(questions)]


async def _round_questions(
    deps: Deps, system: str, user: str, *, warn_state: PrepState, warn_message: str
) -> list[PlannedQuestion]:
    """Shared call + resilient-degrade wrapper for a round's LLM call."""
    try:
        result = await deps.llm.complete_json(system=system, user=user, schema=_RoundQuestions)
        return result.questions
    except Exception as exc:  # noqa: BLE001 - resilient: degrade, don't crash prep
        log.warning("%s (%s)", warn_message, exc)
        await _warn(warn_state, deps, [warn_message])
        return build_mock(_RoundQuestions).questions


def _pin_general_followups(
    questions: list[PlannedQuestion], budget: FollowUpBudget
) -> list[PlannedQuestion]:
    """Cap each technical question's followups to the depth budget, regardless
    of what the model returned. The LAST technical question (the signature/case
    question — same "last technical question" rule the prompt itself uses) gets
    up to ``signature_followups_max``; every other technical question gets up
    to ``technical_followups``. Intro/wrap questions are left untouched — the
    prompt never asks for followups on them.
    """
    technical_indices = [i for i, q in enumerate(questions) if q.section == "technical"]
    if not technical_indices:
        return questions
    signature_idx = technical_indices[-1]
    pinned = list(questions)
    for i in technical_indices:
        cap = (
            budget.signature_followups_max
            if i == signature_idx
            else budget.technical_followups
        )
        pinned[i] = pinned[i].model_copy(update={"followups": list(pinned[i].followups)[:cap]})
    return pinned


async def general_round(state: PrepState, deps: Deps) -> PrepState:
    """GENERAL round: intro + JD-weighted technical questions (anchored by
    one signature/case question with a follow-up tree) + a wrap question.
    """
    req = state["req"]
    job = state["job"]
    candidate = state["candidate"]
    company = state["company"]
    gap = state["gap"]

    family = role_packs.infer_role_family(job)
    band = role_packs.seniority_band(role_packs.infer_seniority(candidate))
    weights = role_packs.infer_competency_weights(family, band, job)
    counts = role_packs.allocate_question_counts(weights, _GENERAL_TECHNICAL_COUNT)
    budget = followup_budget(req.follow_up_depth)

    hint = _skill_library_hint(company=company.name, role=job.title, level=job.seniority)
    system, user = general_round_prompts(
        candidate=candidate,
        job=job,
        company=company,
        gap=gap,
        language_mode=req.language_mode,
        weights=weights,
        counts=counts,
        budget=budget,
        hint=hint,
    )
    questions = await _round_questions(
        deps,
        system,
        user,
        warn_state=state,
        warn_message="Could not tailor the general round; used a minimal set.",
    )

    valid_sections = {"intro", "technical", "wrap"}
    pinned = [
        q.model_copy(update={"section": q.section if q.section in valid_sections else "technical"})
        for q in questions
    ]
    pinned = _pin_general_followups(pinned, budget)
    pinned = _reid(pinned, "gen")
    await _mark(state, deps, "general_round")
    return {"general_questions": pinned}


async def coding_round(state: PrepState, deps: Deps) -> PrepState:
    """CODING round: exactly one spoken, think-aloud problem, topic and
    difficulty chosen deterministically by ``role_packs.select_coding_topic``
    from role family + the candidate's actual stack + inferred seniority.
    """
    req = state["req"]
    job = state["job"]
    candidate = state["candidate"]
    gap = state["gap"]
    company = state["company"]

    family = role_packs.infer_role_family(job)
    seniority = role_packs.infer_seniority(candidate)
    topic, difficulty = role_packs.select_coding_topic(family, job.tech_stack, seniority)
    topic_meta = role_packs.coding_topic_meta(family, topic)
    budget = followup_budget(req.follow_up_depth)

    hint = _skill_library_hint(company=company.name, role=job.title, level=job.seniority)
    system, user = coding_round_prompts(
        candidate=candidate,
        job=job,
        gap=gap,
        language_mode=req.language_mode,
        topic=topic,
        topic_meta=topic_meta,
        difficulty=difficulty,
        followup_count=budget.coding_followups,
        hint=hint,
    )
    questions = await _round_questions(
        deps,
        system,
        user,
        warn_state=state,
        warn_message="Could not tailor the coding round; used a minimal question.",
    )
    # Exactly one problem, always — deterministically pinned regardless of
    # what (or how many) the model returned.
    questions = questions[:1] or build_mock(_RoundQuestions).questions[:1]

    pinned = []
    for q in questions:
        followups = list(q.followups)[: budget.coding_followups] or [topic_meta["hint"]]
        pinned.append(
            q.model_copy(
                update={
                    "section": "coding",
                    "difficulty": difficulty,
                    "target_competency": topic,
                    "followups": followups,
                }
            )
        )
    pinned = _reid(pinned, "code")
    await _mark(state, deps, "coding_round")
    return {"coding_questions": pinned}


async def behavioral_round(state: PrepState, deps: Deps) -> PrepState:
    """BEHAVIORAL round: STAR-style questions grounded in specific CV
    achievements, each with pre-written individual-contribution followups
    pinned into ``followups[:budget.behavioral_followups]``.
    """
    req = state["req"]
    job = state["job"]
    candidate = state["candidate"]
    gap = state["gap"]
    company = state["company"]
    budget = followup_budget(req.follow_up_depth)

    hint = _skill_library_hint(company=company.name, role=job.title, level=job.seniority)
    system, user = behavioral_round_prompts(
        candidate=candidate,
        job=job,
        gap=gap,
        language_mode=req.language_mode,
        count=_BEHAVIORAL_COUNT,
        followup_count=budget.behavioral_followups,
        hint=hint,
    )
    questions = await _round_questions(
        deps,
        system,
        user,
        warn_state=state,
        warn_message="Could not tailor the behavioral round; used a minimal set.",
    )

    pinned = []
    for q in questions:
        followups = list(q.followups)[: budget.behavioral_followups] or [_BEHAVIORAL_FALLBACK_PROBE]
        pinned.append(q.model_copy(update={"section": "behavioral", "followups": followups}))
    pinned = _reid(pinned, "beh")
    await _mark(state, deps, "behavioral_round")
    return {"behavioral_questions": pinned}


async def assemble_plan(state: PrepState, deps: Deps) -> PrepState:
    """Join node: stitch the three rounds' questions into the final
    ``QuestionPlan``, ordered by the fixed section order, with the request's
    language_mode pinned regardless of what any round echoed back.
    """
    req = state["req"]
    all_questions: list[PlannedQuestion] = [
        *state.get("general_questions", []),
        *state.get("behavioral_questions", []),
        *state.get("coding_questions", []),
    ]
    by_section: dict[str, list[PlannedQuestion]] = {s: [] for s in _SECTION_ORDER}
    for q in all_questions:
        by_section.setdefault(q.section, []).append(q)
    ordered = [q for section in _SECTION_ORDER for q in by_section.get(section, [])]

    plan = QuestionPlan(
        sections_order=list(_SECTION_ORDER),
        questions=ordered,
        time_budget_min=_TIME_BUDGET_MIN,
        language_mode=req.language_mode,
    )
    await _mark(state, deps, "assemble_plan")
    return {"plan": plan}
