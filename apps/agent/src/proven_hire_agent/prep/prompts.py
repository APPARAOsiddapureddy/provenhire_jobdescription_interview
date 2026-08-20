"""Prompt builders for the prep graph nodes.

English-first: every system prompt is in English, but where a node produces
``LocalizedText`` (the question planner) it instructs the model to fill BOTH the
``en`` entry and the candidate's primary language. The mock LLM ignores prompt
content entirely, so these strings only matter once a real provider is wired.

Each builder returns a ``(system, user)`` tuple. ``user`` payloads are compact,
model-readable summaries of upstream state so the live agent prompt downstream
stays small (per the project's prep/live/post split).
"""

from __future__ import annotations

from ..shared_models import (
    CandidateProfile,
    CompanyIntel,
    GapAnalysis,
    JobSpec,
    LanguageMode,
    PrepRequest,
)
from .follow_up_depth import FollowUpBudget

# Human-readable language names for the small set we support, so prompts can say
# "also write Vietnamese" rather than leaking the raw code to the model.
_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "vi": "Vietnamese",
    "es": "Spanish",
    "zh": "Chinese",
    "hi": "Hindi",
    "id": "Indonesian",
    "pt": "Portuguese",
    "fr": "French",
    "de": "German",
    "ja": "Japanese",
}


def language_name(code: str) -> str:
    """Return a human-readable language name for ``code`` (defaults to the code)."""
    return _LANGUAGE_NAMES.get(code, code)


# --- cv analysis -------------------------------------------------------------


def cv_analysis_prompts(cv_text: str) -> tuple[str, str]:
    """System/user prompts to extract a ``CandidateProfile`` from raw CV text."""
    system = (
        "You are a meticulous technical recruiter. Read the candidate's CV/resume "
        "text and extract a structured profile. Infer seniority from years of "
        "experience and scope of work. Be faithful to the source; do not invent "
        "employers, degrees, or skills that are not supported by the text. Keep "
        "summary_120w to roughly 120 words. Respond ONLY with the requested schema."
    )
    user = f"CANDIDATE CV / RESUME TEXT:\n{cv_text}"
    return system, user


# --- jd analysis -------------------------------------------------------------


def jd_analysis_prompts(jd_text: str, company: str) -> tuple[str, str]:
    """System/user prompts to extract a ``JobSpec`` from the job description."""
    system = (
        "You are a hiring manager. Parse the job description into a structured job "
        "spec: title, seniority, must-have vs nice-to-have requirements, core "
        "responsibilities, and the technology stack. Preserve the original text in "
        "raw_text. Do not fabricate requirements the description does not state. "
        "Respond ONLY with the requested schema."
    )
    user = f"TARGET COMPANY: {company}\n\nJOB DESCRIPTION:\n{jd_text}"
    return system, user


# --- company research --------------------------------------------------------


def company_research_prompts(company: str, snippets: str) -> tuple[str, str]:
    """System/user prompts to synthesize ``CompanyIntel`` from search snippets."""
    system = (
        "You are an interview-prep researcher. Using ONLY the provided web search "
        "snippets, summarize what a candidate should know before interviewing: a "
        "short company summary, industry, likely technology stack, stated values, "
        "the typical interview process and stages, and any recent news. If a field "
        "is not supported by the snippets, leave its list empty rather than "
        "guessing. Leave citations empty; the pipeline fills them. Respond ONLY "
        "with the requested schema."
    )
    user = f"COMPANY: {company}\n\nWEB SEARCH SNIPPETS:\n{snippets}"
    return system, user


# --- gap matching ------------------------------------------------------------


def gap_matching_prompts(candidate: CandidateProfile, job: JobSpec) -> tuple[str, str]:
    """System/user prompts comparing the candidate against the job requirements."""
    system = (
        "You are an interview strategist. Compare the candidate against the job "
        "requirements. Identify genuine strengths, gaps, and the specific topics an "
        "interviewer should PROBE to confirm or disprove fit (probe_targets). List "
        "which required skills are matched vs missing. Be concrete and evidence-led. "
        "Respond ONLY with the requested schema."
    )
    user = (
        "CANDIDATE SUMMARY:\n"
        f"- name: {candidate.name}\n"
        f"- headline: {candidate.headline}\n"
        f"- seniority: {candidate.seniority} ({candidate.years_experience}y)\n"
        f"- skills: {', '.join(candidate.skills)}\n"
        f"- achievements: {'; '.join(candidate.achievements)}\n\n"
        "JOB REQUIREMENTS:\n"
        f"- title: {job.title} at {job.company_name}\n"
        f"- seniority: {job.seniority}\n"
        f"- must_have: {', '.join(job.must_have)}\n"
        f"- nice_to_have: {', '.join(job.nice_to_have)}\n"
        f"- tech_stack: {', '.join(job.tech_stack)}\n"
        f"- responsibilities: {'; '.join(job.responsibilities)}"
    )
    return system, user


# --- round-separated question generation --------------------------------------
#
# Three focused prompts replace the old single `question_planner_prompts()`
# keystone call — one per live round (general, coding, behavioral). Each
# takes deterministic role-pack selections (role family, competency weights/
# counts, coding topic/difficulty) computed by `role_packs.py` *before* the
# call, so the LLM is told exactly what to produce rather than asked to
# infer it fresh; `nodes.py` pins the same selections back onto the result
# afterward regardless of what the model returns (see its module docstring).


def _localize_note(language_mode: LanguageMode) -> str:
    primary = language_mode.primary
    primary_name = language_name(primary)
    also_localize = (
        ""
        if primary == "en"
        else (
            f" In addition to the required 'en' entry, also provide a '{primary}' "
            f"({primary_name}) translation for every question's text."
        )
    )
    mixed_note = (
        " The interview may code-switch between English and the primary language."
        if language_mode.mixed
        else ""
    )
    return f"{also_localize}{mixed_note}"


def general_round_prompts(
    candidate: CandidateProfile,
    job: JobSpec,
    company: CompanyIntel,
    gap: GapAnalysis,
    language_mode: LanguageMode,
    weights: dict[str, float],
    counts: dict[str, int],
    budget: FollowUpBudget,
    hint: str = "",
) -> tuple[str, str]:
    """System/user prompts for the GENERAL round: intro + technical + wrap
    questions, JD-weighted across the given competency allocation, anchored
    by one deep signature/case question with a branching follow-up tree.

    ``budget`` (see ``follow_up_depth.py``) sets how many followups ordinary
    technical questions vs. the signature question should carry; ``nodes.py``
    pins the model's output back to these counts regardless of what it returns.
    """
    allocation_lines = "\n".join(
        f'- {n} question(s) targeting competency "{comp}"'
        for comp, n in counts.items()
        if n > 0
    )
    ordinary_followup_line = (
        "- followups: leave ordinary technical questions with NO followup "
        "entries (empty list) — this interview asks only the scripted "
        "question for those.\n"
        if budget.technical_followups == 0
        else (
            f"- followups: exactly {budget.technical_followups} entr"
            f"{'y' if budget.technical_followups == 1 else 'ies'} per ordinary "
            "technical question (a concrete probe, not 'can you elaborate').\n"
        )
    )
    system = (
        "You are a senior interviewer designing the GENERAL / TECHNICAL round "
        "of a mock interview (sections: intro, technical, wrap). Write ONE "
        "intro warm-up question, then the technical questions below, then ONE "
        "closing/wrap question.\n\n"
        "ALLOCATION (fixed — do not deviate from these counts):\n"
        f"{allocation_lines}\n\n"
        "HARD RULES:\n"
        "- Every technical question must be concretely tailored to THIS "
        "candidate and THIS job/company: use real table/field/metric names, "
        "the company's actual domain, and numbers drawn from the JD or "
        "company intel where plausible. NEVER ask a generic textbook "
        "definition question (e.g. 'what is a JOIN') in isolation — if a "
        "fundamental must be tested, wrap it in a scenario using this "
        "company's data/domain.\n"
        "- target_competency for each technical question MUST be exactly one "
        "of the competency keys named in the allocation above.\n"
        "- Rising difficulty 1-5 across the technical section.\n"
        "- BREVITY: the candidate HEARS each question spoken aloud, not read on "
        "a screen — keep each question's text to ONE clear ask, roughly one "
        "sentence (~30 words or less). If a fully-tailored question would "
        "naturally need two things asked at once (e.g. 'explain your schema "
        "AND how you'd index it'), split it into two separate questions "
        "instead of one long compound sentence — never sacrifice specificity, "
        "just don't stack multiple asks into a single utterance.\n"
        "- rubric: 2-4 RubricItems, weights summing to ~1.0. Each criterion's "
        "description must state (a) the concept(s) a strong answer names "
        "concretely, (b) one strong-hire signal (a specific phrase/behavior), "
        "and (c) one red flag (a specific weak/wrong answer pattern) — like a "
        "real interviewer's notes, not a rubric template.\n"
        f"{ordinary_followup_line}"
        "- SIGNATURE QUESTION: make the LAST technical question a deeper, "
        "end-to-end case/system question anchored in this company's actual "
        "product/domain — the single hardest, most discriminating question in "
        "the interview. Keep its own text just as short/single-focus as the "
        "others (one clear starting point, per the BREVITY rule above) — the "
        "'multi-part' depth lives in the follow-up tree below, not stacked "
        "into one long opening sentence. Give it followups as an ORDERED "
        "ADAPTIVE FOLLOW-UP TREE: "
        f"{budget.signature_followups_min}-{budget.signature_followups_max} "
        "entries, each a natural next probe building on the previous, "
        "covering distinct sub-topics rather than repeating the same angle.\n"
        "- LENIENCY: topics flagged NICE-TO-HAVE below should be probed "
        "supportively (framed as 'how would you approach/learn X'), not used "
        "as a hard rejection axis; topics flagged MUST-HAVE should be probed "
        "rigorously.\n"
        f"Write each question's text with an 'en' entry.{_localize_note(language_mode)} "
        "Respond ONLY with the requested schema."
    )
    user = (
        f"ROLE: {job.title} ({job.seniority}) at {company.name}\n"
        f"MUST-HAVE (probe rigorously): {', '.join(job.must_have)}\n"
        f"NICE-TO-HAVE (probe leniently): {', '.join(job.nice_to_have)}\n"
        f"RESPONSIBILITIES: {'; '.join(job.responsibilities)}\n"
        f"TECH STACK: {', '.join(job.tech_stack)}\n\n"
        f"COMPANY: {company.summary}\n"
        f"COMPANY VALUES/PROCESS: {', '.join(company.values)}; "
        f"{' -> '.join(company.interview_process)}\n\n"
        f"CANDIDATE: {candidate.headline}; {candidate.years_experience}y; "
        f"skills: {', '.join(candidate.skills)}; "
        f"achievements: {'; '.join(candidate.achievements)}\n\n"
        "GAP TO PROBE:\n"
        f"- probe_targets: {', '.join(gap.probe_targets)}\n"
        f"- missing_skills: {', '.join(gap.missing_skills)}\n"
        f"- matched_skills (confirm briefly, don't over-index): {', '.join(gap.matched_skills)}\n\n"
        + (f"PLAYBOOK REFERENCE:\n{hint}\n\n" if hint else "")
        + "Design the intro + technical + wrap questions now."
    )
    return system, user


def coding_round_prompts(
    candidate: CandidateProfile,
    job: JobSpec,
    gap: GapAnalysis,
    language_mode: LanguageMode,
    topic: str,
    topic_meta: dict,
    difficulty: int,
    followup_count: int,
    hint: str = "",
) -> tuple[str, str]:
    """System/user prompts for the CODING round: exactly one spoken,
    think-aloud problem grounded in a deterministically-selected topic +
    difficulty (see ``role_packs.select_coding_topic``). ``followup_count``
    (see ``follow_up_depth.py``) sets how many hint entries to request.
    """
    followup_word = "y" if followup_count == 1 else "ies"
    system = (
        "You are a senior interviewer designing the CODING round: exactly ONE "
        "spoken, think-aloud problem. There is NO code editor — the candidate "
        "talks through their reasoning out loud, like a phone screen, not a "
        "leetcode session.\n\n"
        f"- Ground the problem in the '{topic}' topic ({topic_meta['description']}), "
        f"using the candidate's ACTUAL stack ({', '.join(job.tech_stack)}) — never "
        "a generic algorithms-textbook prompt disconnected from the JD.\n"
        f"- Target difficulty {difficulty}/5 for a {job.seniority}-level candidate "
        "at this scope. Favor reasoning and trade-off talk over memorized syntax "
        "or DSA trivia — this is not a whiteboard-coding round.\n"
        "- BREVITY: state the problem in ONE or TWO short sentences, under 40 "
        "words total — the candidate hears this spoken aloud, not read off a "
        "screen. Give only the minimum setup needed to understand the ask; "
        "save extra detail (schema specifics, constraints) for a follow-up if "
        "the candidate asks, don't front-load it all into the opening.\n"
        "- rubric: 2-3 RubricItems whose descriptions state the reasoning steps "
        "expected OUT LOUD (e.g. 'names the failure mode before proposing a "
        "fix'), a strong-hire signal, and a red flag (e.g. 'jumps straight to "
        "\"add more servers\"/\"use Redis, it's faster\" without diagnosing "
        "first') — NOT a 'correct syntax' criterion, since there is no editor.\n"
        f"- followups: write EXACTLY {followup_count} entr{followup_word} — "
        "hint(s) to give if the candidate stalls, each a nudge toward the "
        "next reasoning step, never the answer itself, each one going a "
        "little further than the last if there is more than one. The live "
        "interviewer uses these verbatim instead of improvising.\n"
        f"Write the question's text with an 'en' entry.{_localize_note(language_mode)} "
        "Respond ONLY with the requested schema (a single question)."
    )
    user = (
        f"ROLE: {job.title} ({job.seniority})\n"
        f"TECH STACK: {', '.join(job.tech_stack)}\n"
        f"CANDIDATE SKILLS: {', '.join(candidate.skills)}\n"
        f"RELEVANT GAP TO PROBE (if applicable to this topic): {', '.join(gap.probe_targets)}\n"
        f"FALLBACK HINT STYLE (for tone/specificity only, write your own): {topic_meta['hint']}"
        + (f"\n\nPLAYBOOK REFERENCE:\n{hint}" if hint else "")
    )
    return system, user


def behavioral_round_prompts(
    candidate: CandidateProfile,
    job: JobSpec,
    gap: GapAnalysis,
    language_mode: LanguageMode,
    count: int,
    followup_count: int,
    hint: str = "",
) -> tuple[str, str]:
    """System/user prompts for the BEHAVIORAL round: ``count`` STAR-style
    questions, each grounded in a specific CV achievement/project, each with
    ``followup_count`` (see ``follow_up_depth.py``) pre-written
    individual-contribution followups pinned into ``followups[:followup_count]``.
    """
    followup_word = "y" if followup_count == 1 else "ies"
    system = (
        f"You are a senior interviewer designing the BEHAVIORAL round: {count} "
        "STAR-style question(s), one story at a time ('Tell me about a time "
        "you...'), each GROUNDED IN A SPECIFIC achievement or project from the "
        "candidate's CV below — never a generic story prompt untethered from "
        "anything they actually did.\n\n"
        "- Prefer target_competency values about judgment, ownership, "
        "collaboration, or leadership (from gap.gaps / gap.probe_targets) "
        "rather than raw technical skill — that belongs in the other rounds.\n"
        "- BREVITY: keep the question itself to one short sentence — the "
        "candidate hears it spoken aloud, not read on a screen.\n"
        f"- followups: write EXACTLY {followup_count} entr{followup_word} per "
        "question — probes for the candidate's OWN specific decision or "
        "action, phrased to pull the 'I' out of the 'we' (e.g. 'What did YOU "
        "specifically decide/say/do here — not what the team decided?'), each "
        "one pushing further than the last if there is more than one. The "
        "live interviewer asks these verbatim as its followups.\n"
        "- rubric: 2-3 RubricItems; descriptions should reward concreteness of "
        "the candidate's individual contribution (names their own specific "
        "decision, can explain the reasoning) and flag the red flag of "
        "team-outcome attribution without a personal contribution.\n"
        f"Write each question's text with an 'en' entry.{_localize_note(language_mode)} "
        "Respond ONLY with the requested schema."
    )
    projects = "; ".join(f"{p.name} — {p.description}" for p in candidate.projects)
    user = (
        f"ROLE: {job.title} ({job.seniority}) at {job.company_name}\n"
        f"CANDIDATE: {candidate.headline}\n"
        f"ACHIEVEMENTS: {'; '.join(candidate.achievements)}\n"
        f"PROJECTS: {projects}\n\n"
        f"BEHAVIORAL GAPS TO PROBE:\n- gaps: {', '.join(gap.gaps)}\n"
        f"- probe_targets: {', '.join(gap.probe_targets)}"
        + (f"\n\nPLAYBOOK REFERENCE:\n{hint}" if hint else "")
    )
    return system, user


def _job_user_payload(req: PrepRequest) -> str:
    """Compact JD payload used by the jd_analysis node (company + raw text)."""
    return f"TARGET COMPANY: {req.company}\n\nJOB DESCRIPTION:\n{req.jd_text}"
