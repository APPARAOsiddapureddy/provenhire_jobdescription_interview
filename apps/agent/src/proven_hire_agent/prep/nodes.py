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

import re
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
    RubricItem,
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
    # Carried through to assemble_plan's competency-coverage check — the
    # allocation this round was actually GIVEN, so a shortfall (e.g. from
    # dedup removing the only question on a must-have competency) is
    # detectable instead of silently shipping.
    return {"general_questions": pinned, "technical_counts": counts}


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


# --- post-generation quality pass -------------------------------------------
#
# The three rounds above are independent LLM calls that run IN PARALLEL and
# cannot see each other's output, and even within one round nothing was
# previously checking that the model actually followed its own instructions
# (rising difficulty, rubric weights summing to ~1.0). This section is a
# deterministic, no-extra-LLM-call pass that catches the failure modes that
# fall out of that: duplicate questions across rounds, unnormalized rubric
# weights (which scoring reads directly), and technical difficulty that
# doesn't actually rise. Nothing here can fail closed — every function
# degrades to "leave the questions as they are" rather than raising, since a
# quality pass must never be the reason prep fails.

# Word-overlap (Jaccard) similarity, not character-level (difflib
# SequenceMatcher). Measured against real question pairs before picking this:
# character-level matching is fooled by word reordering — "tell me about a
# challenging project" vs "tell me about a project that was challenging"
# scored LOWER (0.69) than two genuinely different behavioral questions that
# merely share the same scaffold ("tell me about a time you led a project"
# vs "...you mentored a junior engineer", 0.70) — i.e. it would have missed
# the real duplicate while risking a false positive on unrelated questions.
# Word-overlap cleanly separates them (0.82 vs 0.43) because it only cares
# WHICH words appear, not their order.
#
# Known limitation, accepted rather than hidden: this cannot catch a
# duplicate reworded with different vocabulary (e.g. "disagreed with a
# teammate" vs "had a conflict with a colleague" scores 0.20 — genuinely the
# same question, but not flagged). Catching that needs semantic/embedding
# similarity, which means another network call; out of scope for a
# deterministic, no-extra-LLM-call pass. This catches the reordering/
# rewording case, which is what's actually been observed.
_DUPLICATE_SIMILARITY_THRESHOLD = 0.65
_RUBRIC_WEIGHT_TOLERANCE = 0.05


def _normalize_for_comparison(text: str) -> str:
    """Lowercase + strip punctuation so two questions that differ only in
    phrasing (not substance) are still recognized as the same ask."""
    lowered = text.lower()
    stripped = re.sub(r"[^\w\s]", "", lowered)
    return re.sub(r"\s+", " ", stripped).strip()


def _word_overlap_similarity(a: str, b: str) -> float:
    """Jaccard similarity over word sets: |intersection| / |union|. See the
    comment on _DUPLICATE_SIMILARITY_THRESHOLD for why this beats
    character-level matching for this specific job."""
    words_a, words_b = set(a.split()), set(b.split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def _question_text(q: PlannedQuestion, primary_lang: str) -> str:
    return q.text.get(primary_lang) or q.text.get("en") or next(iter(q.text.values()), "")


# The coding round is a structural invariant (see test_prep.py: exactly ONE
# coding-round question is REQUIRED — that's the interview's single live
# think-aloud problem, deterministically chosen by role_packs, not optional
# content). It must never be the one DROPPED by a text-similarity coincidence
# — found by testing, not assumed: under MockLLM every round's placeholder
# text is literally identical ("mock"), so coding's question (processed
# after technical, per _SECTION_ORDER) collided with an earlier technical
# question's text and got silently deleted, breaking that invariant. Exempt
# from being dropped, not from the comparison entirely — a later question
# duplicating a coding question can still be correctly flagged against it.
_DEDUPE_EXEMPT_SECTIONS = {"coding"}


def _dedupe_questions(
    questions: list[PlannedQuestion], primary_lang: str
) -> tuple[list[PlannedQuestion], list[str]]:
    """Drop later near-duplicates. ``questions`` is already in
    ``_SECTION_ORDER`` (intro, behavioral, technical, coding, wrap), so "keep
    the first occurrence" naturally keeps the EARLIER-asked copy and drops
    whichever later round redundantly re-asked the same thing — e.g. general
    and behavioral both independently landing on "tell me about a challenging
    project you worked on." Returns (kept_questions, dropped_descriptions) so
    the caller can log what happened.
    """
    kept: list[PlannedQuestion] = []
    kept_norm: list[str] = []
    dropped: list[str] = []
    for q in questions:
        norm = _normalize_for_comparison(_question_text(q, primary_lang))
        if not norm or q.section in _DEDUPE_EXEMPT_SECTIONS:
            kept.append(q)
            kept_norm.append(norm)
            continue
        is_dup = any(
            _word_overlap_similarity(norm, prior) >= _DUPLICATE_SIMILARITY_THRESHOLD
            for prior in kept_norm
            if prior
        )
        if is_dup:
            dropped.append(f"{q.section}/{q.id}: {_question_text(q, primary_lang)[:80]}")
            continue
        kept.append(q)
        kept_norm.append(norm)
    return kept, dropped


def _normalize_rubric_weights(questions: list[PlannedQuestion]) -> list[PlannedQuestion]:
    """Rescale each question's rubric weights to sum to 1.0, proportionally
    (preserving the model's relative emphasis across criteria). Scoring reads
    these weights directly — an unchecked miss here (weights summing to 1.6,
    or 0.4) silently distorts a candidate's score with nothing to catch it.
    """
    fixed: list[PlannedQuestion] = []
    for q in questions:
        if not q.rubric:
            fixed.append(q)
            continue
        total = sum(item.weight for item in q.rubric)
        if abs(total - 1.0) <= _RUBRIC_WEIGHT_TOLERANCE:
            fixed.append(q)
            continue
        rescaled: list[RubricItem]
        if total > 0:
            rescaled = [item.model_copy(update={"weight": item.weight / total}) for item in q.rubric]
        else:
            even = 1.0 / len(q.rubric)
            rescaled = [item.model_copy(update={"weight": even}) for item in q.rubric]
        fixed.append(q.model_copy(update={"rubric": rescaled}))
    return fixed


def _reorder_technical_by_difficulty(questions: list[PlannedQuestion]) -> list[PlannedQuestion]:
    """Best-effort repair for the prompt's "rising difficulty 1-5 across the
    technical section" rule, which nothing previously enforced.

    The LAST technical question is the signature/case question (see
    ``_pin_general_followups``, which already gave it the deep follow-up
    tree by that same "last technical question" rule) and is deliberately
    left in place regardless of its own difficulty score — moving it would
    strand its follow-up tree in the middle of the section, which is a worse
    outcome than tolerating one non-monotonic step right before it.
    """
    technical_idx = [i for i, q in enumerate(questions) if q.section == "technical"]
    if len(technical_idx) < 2:
        return questions
    signature_idx = technical_idx[-1]
    reorderable_idx = [i for i in technical_idx if i != signature_idx]
    reordered = sorted((questions[i] for i in reorderable_idx), key=lambda q: q.difficulty)
    result = list(questions)
    for pos, q in zip(reorderable_idx, reordered, strict=True):
        result[pos] = q
    return result


def _check_competency_coverage(
    questions: list[PlannedQuestion], expected_counts: dict[str, int]
) -> list[str]:
    """Detection only — fixing a shortfall would mean generating a NEW
    question, which is a real LLM call and out of scope for a deterministic
    pass. Surfacing it as a warning at least makes a silent coverage gap
    (e.g. dedup removed the only question probing a must-have competency)
    visible instead of invisible.
    """
    actual: dict[str, int] = {}
    for q in questions:
        if q.section == "technical":
            actual[q.target_competency] = actual.get(q.target_competency, 0) + 1
    warnings: list[str] = []
    for competency, expected in expected_counts.items():
        got = actual.get(competency, 0)
        if expected > 0 and got < expected:
            warnings.append(
                f"Competency '{competency}' expected {expected} technical "
                f"question(s) but the plan has {got} (after quality checks)."
            )
    return warnings


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

    # Deterministic quality pass — see the block above assemble_plan for why
    # this exists. Order matters: dedupe first (so coverage is checked
    # against what the candidate will ACTUALLY be asked), then normalize
    # rubric weights, then repair difficulty ordering.
    original_count = len(ordered)
    deduped, dropped = _dedupe_questions(ordered, req.language_mode.primary)

    quality_warnings: list[str] = []
    # Two independent safety checks before TRUSTING the dedup result — this
    # pass exists to trim the occasional real overlap between rounds, never
    # to restructure the interview:
    #
    # 1. A GLOBAL cap: removing an implausible SHARE of all questions signals
    #    something systemically wrong upstream (e.g. a degraded LLM
    #    returning near-identical boilerplate for every question), not
    #    genuine duplication.
    # 2. A PER-SECTION floor: found by testing, not assumed — the global cap
    #    alone missed a real case. Dropping just ONE question out of ~14
    #    total stayed comfortably under any global percentage, but that one
    #    question was the entire behavioral section's only question, so the
    #    section was silently emptied while the global check saw nothing
    #    wrong. A section that started non-empty must never end empty,
    #    regardless of how small the overall removal looks.
    #
    # Either check failing bails out of dedup ENTIRELY for this plan, rather
    # than trying to selectively keep back just enough questions — a few
    # undetected real duplicates is a far smaller problem than an interview
    # missing most, or all, of a section.
    max_droppable = max(2, round(original_count * 0.25))
    before_counts: dict[str, int] = {}
    after_counts: dict[str, int] = {}
    for q in ordered:
        before_counts[q.section] = before_counts.get(q.section, 0) + 1
    for q in deduped:
        after_counts[q.section] = after_counts.get(q.section, 0) + 1
    emptied_sections = [
        section
        for section, before in before_counts.items()
        if before > 0 and after_counts.get(section, 0) == 0
    ]

    if len(dropped) > max_droppable or emptied_sections:
        reason = (
            f"would have emptied section(s) {emptied_sections}"
            if emptied_sections
            else f"an implausible share ({len(dropped)}/{original_count})"
        )
        quality_warnings.append(
            f"Dedup skipped for this plan — {reason} — likely a systemic "
            "LLM/content issue rather than genuine cross-round overlap. "
            "Review the generated questions manually."
        )
    else:
        ordered = deduped
        if dropped:
            quality_warnings.append(
                f"Removed {len(dropped)} near-duplicate question(s) generated "
                f"across rounds: {'; '.join(dropped)}"
            )

    ordered = _normalize_rubric_weights(ordered)
    ordered = _reorder_technical_by_difficulty(ordered)
    quality_warnings.extend(
        _check_competency_coverage(ordered, state.get("technical_counts", {}))
    )
    if quality_warnings:
        log.warning("assemble_plan quality pass: %s", "; ".join(quality_warnings))
        await _warn(state, deps, quality_warnings)

    plan = QuestionPlan(
        sections_order=list(_SECTION_ORDER),
        questions=ordered,
        time_budget_min=_TIME_BUDGET_MIN,
        language_mode=req.language_mode,
    )
    await _mark(state, deps, "assemble_plan")
    return {"plan": plan}
