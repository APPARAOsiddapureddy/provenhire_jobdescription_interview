# Question Generation Spec — General / Coding / Behavioral rounds

*What decides which questions the AI interviewer asks, and how each maps to a
scored competency.*

## TL;DR

- **One keystone LLM call** (`question_planner`, prep phase) builds the
  **entire** `QuestionPlan` — all five sections (`intro`, `behavioral`,
  `technical`, `coding`, `wrap`) — in a single generic prompt. This part is
  real and wired end-to-end: CV + JD + company research + gap analysis go in,
  a scored `QuestionPlan` comes out, and post-call scoring reads it back via
  `target_competency` to build the report.
- **Round personas exist only at live-call time**, not at plan-generation
  time. `CodingRoundAgent` and `BehavioralAgent`
  (`apps/agent/src/proven_hire_agent/live/handoffs.py`) each carry a one-off
  system-prompt instruction ("nudge with a single hint if they stall",
  "exactly one probing follow-up... the 'I' not the 'we'") that shapes *how
  the live model behaves*, but nothing upstream in prep ever selects a coding
  problem by topic/difficulty, or writes a behavioral question with a
  pre-built individual-contribution follow-up. That logic doesn't exist yet.
- Net: **general-round question targeting is implemented** (skill-gap-driven,
  scored, traceable). **Coding-round problem selection and
  behavioral/STAR follow-up design are not implemented as prep-time logic** —
  today they're just "whatever the one big prompt happened to produce for
  that section," steered only by a same-turn live instruction. §5 and §6
  below spec and draft what's missing, using only existing contract types.

---

## 1. Where this lives today

| Concern | File | What's there |
|---|---|---|
| Question-plan prompt | [`apps/agent/src/proven_hire_agent/prep/prompts.py:128-184`](../apps/agent/src/proven_hire_agent/prep/prompts.py) | `question_planner_prompts()` — the single system/user prompt that produces the whole plan |
| Question-plan node | [`apps/agent/src/proven_hire_agent/prep/nodes.py:266-306`](../apps/agent/src/proven_hire_agent/prep/nodes.py) | `question_planner()` — calls the LLM, injects an optional skill-library hint, pins `language_mode` |
| Graph wiring | [`apps/agent/src/proven_hire_agent/prep/graph.py`](../apps/agent/src/proven_hire_agent/prep/graph.py) | `gap_matching -> question_planner -> END` (single sequential keystone step, no per-round fan-out) |
| Gap analysis (feeds the planner) | `prep/prompts.py:98-122` (`gap_matching_prompts`) | Produces `strengths` / `gaps` / `probe_targets` / `matched_skills` / `missing_skills` |
| Data contracts | [`packages/shared/src/question.ts`](../packages/shared/src/question.ts), [`gap.ts`](../packages/shared/src/gap.ts), [`score.ts`](../packages/shared/src/score.ts) | `PlannedQuestion`, `QuestionPlan`, `GapAnalysis`, `CompetencyScore` — mirrored as Pydantic in `apps/agent/src/proven_hire_agent/shared_models.py` |
| Round personas (live, turn-level only) | [`apps/agent/src/proven_hire_agent/live/handoffs.py`](../apps/agent/src/proven_hire_agent/live/handoffs.py) | `CodingRoundAgent`, `BehavioralAgent` — one instruction string each, applied on top of whatever question the plan handed them |
| Adaptive difficulty (live, generic) | [`apps/agent/src/proven_hire_agent/live/state.py:237-329`](../apps/agent/src/proven_hire_agent/live/state.py) | `evaluate_difficulty()` — pure word-count heuristic per section, not coding/behavioral-specific |
| Competency scoring (post) | [`apps/agent/src/proven_hire_agent/post/prompts.py:45-74`](../apps/agent/src/proven_hire_agent/post/prompts.py) | `evaluate_answer_prompts()` — grades each answer against `question.target_competency` + `question.rubric`, feeding `ScoreCard.competency_scores` |
| Company/role playbooks (optional planner context) | [`skills/*.md`](../skills) | Hand-curated question banks per company+role+level, injected as a hint (§4) |

## 2. The `PlannedQuestion` contract (unchanged by anything proposed here)

```ts
// packages/shared/src/question.ts
PlannedQuestion = {
  id: string;
  section: "intro" | "behavioral" | "technical" | "coding" | "wrap";
  text: LocalizedText;          // { en: "...", vi?: "...", ... }
  difficulty: number;           // int, 1-5
  rubric: RubricItem[];         // { criterion, weight, description }, weights ~sum to 1.0
  followups: string[];          // seeded follow-up prompts
  target_competency: string;    // free-text key, e.g. "distributed_systems"
}
```

`target_competency` is the load-bearing field for the whole loop: it's what
`post/prompts.py:evaluate_answer_prompts()` scores against, and what shows up
verbatim as `ScoreCard.competency_scores[].competency` in the report. Every
proposal below reuses this field — nothing new is added to the schema.

## 3. How a question is chosen today (the one call that does everything)

`question_planner()` sends **one** prompt covering all five sections at once.
The actual system prompt (`prep/prompts.py:151-166`):

```
You are a senior interviewer designing a structured ~15 minute mock interview.
Produce a question plan that:
- Orders sections across intro, behavioral, technical, coding, and wrap.
- Follows a RISING difficulty curve scored 1-5 (start easy, ramp up).
- Gives every question a target_competency drawn from the gap analysis
  (prioritise probe_targets and missing_skills).
- Attaches a scoring rubric of 1-3 RubricItems per question whose weights
  sum to about 1.0.
- Seeds at least one followup probe per question.
- Sets time_budget_min to about 15 and language_mode as given.
- Tailors questions to the candidate's background and the company's
  interview process and values.
Write each question's text with an 'en' entry. [+ localize] [+ code-switch note]
Respond ONLY with the requested schema.
```

Inputs (the user message, `prep/prompts.py:167-183`): job title/seniority/company,
primary language, candidate headline + years + skills, company values +
interview process + tech stack, and the full gap analysis (strengths, gaps,
probe_targets, missing_skills). Optionally a **skill-library hint** — up to
1,500 chars pulled from a matching `skills/*.md` playbook's *Round structure /
Question bank / Signals / Pitfalls* sections (`nodes.py:216-263`), e.g.
[`skills/generic-backend-engineer-senior.md`](../skills/generic-backend-engineer-senior.md),
matched by company+role+level.

This is where the **general round's** skill-gap targeting genuinely lives —
"prioritise probe_targets and missing_skills" is a real instruction, not
aspirational. It's just not *separated* from coding/behavioral; one call does
all three at once with one shared, generic rulebook.

## 4. Competency mapping, end to end (already wired, all rounds)

```
gap_matching → GapAnalysis{probe_targets, missing_skills, gaps}
      ↓
question_planner → PlannedQuestion.target_competency (free-text, LLM-chosen)
      ↓ (live call answers it, saved as AnswerRecord)
post/evaluator.py → evaluate_answer_prompts(question, answer)
      ↓ scores 0-5 against question.rubric, keyed by question.target_competency
ScoreCard.competency_scores[] → { competency, score, evidence, level }
      ↓
ScoreCard.weak_competencies → feeds the next prep cycle's coach
```

This loop is round-agnostic and works today regardless of section — it's the
mechanism, not the per-round question quality, that's solid.

---

## 5. Round-by-round spec

### 5.1 General interviewer

**Status: implemented, generic.** Skill-gap targeting is real (§3), scoring
loop is real (§4). What's missing is only that it isn't its own prompt — it's
one-fifth of the monolithic call, competing with coding/behavioral for the
model's attention budget in a single JSON response.

- **(a) Prompt** — see §3 verbatim; general questions come from `section:
  "intro"` / `"technical"` slots of that same output.
- **(b) Inputs** — `CandidateProfile`, `JobSpec`, `CompanyIntel`,
  `GapAnalysis` (all of it).
- **(c) Competency mapping** — `target_competency` is instructed to draw from
  `gap.probe_targets` and `gap.missing_skills` specifically (not just any
  matched skill), so a candidate strong everywhere and weak in one area
  should get proportionally more questions probing that area. There's no
  explicit *quota* enforcing this today (e.g. "at least N technical questions
  must target a `probe_target`") — it's a instruction, not a constraint.

**Proposed tightening** (draft — same schema, no new fields): make the
existing instruction a hard constraint instead of a soft preference:

```python
def general_round_prompts(
    candidate: CandidateProfile, job: JobSpec, company: CompanyIntel, gap: GapAnalysis,
    language_mode: LanguageMode,
) -> tuple[str, str]:
    system = (
        "You are a senior interviewer running the GENERAL / TECHNICAL round of a "
        "mock interview (sections: intro, technical). Write 3-5 questions.\n"
        "- At least half of all technical questions MUST have target_competency "
        "equal to one of gap.probe_targets or gap.missing_skills, verbatim.\n"
        "- The remaining questions may target gap.matched_skills to confirm "
        "genuine strengths — do not pad with unrelated generic questions.\n"
        "- Rising difficulty 1-5. Rubric: 1-3 items, weights ~sum to 1.0. "
        "At least one followup per question.\n"
        "Respond ONLY with the requested schema."
    )
    user = (
        f"ROLE: {job.title} ({job.seniority}) at {company.name}\n"
        f"CANDIDATE: {candidate.headline}; skills: {', '.join(candidate.skills)}\n\n"
        f"GAP TO PROBE:\n- probe_targets: {', '.join(gap.probe_targets)}\n"
        f"- missing_skills: {', '.join(gap.missing_skills)}\n"
        f"- matched_skills (confirm, don't over-index): {', '.join(gap.matched_skills)}"
    )
    return system, user
```

---

### 5.2 Coding round

**Status: not implemented as prep-time logic.** The only coding-specific
behavior in the whole codebase is this live-turn instruction
(`live/handoffs.py:23-28`):

```python
_CODING_INSTRUCTIONS = (
    "You are now running the CODING round. Pose one focused, hands-on problem "
    "tied to the candidate's stack. Ask them to think aloud; nudge with a single "
    "hint if they stall. Do not lecture. When the problem is resolved or time is "
    "tight, call save_answer then get_next_question."
)
```

This is applied to *whatever question happens to have `section: "coding"`* in
the monolithic plan — there is no code anywhere that (1) picks a topic from
the candidate's actual stack, (2) bands difficulty off seniority specifically
for coding (the rising-curve rule in §3 is global, not coding-aware), or (3)
pre-writes the "one hint" so it's reproducible rather than improvised fresh
by the live model every time.

**(a) Proposed prompt + selection logic** (draft — fits existing
`PlannedQuestion`/`RubricItem`, no new fields; adds one small deterministic
pre-step in Python before the LLM call, same pattern already used for
company-name validation in `nodes.py`):

```python
# prep/prompts.py

# A small, explicit topic taxonomy so coding-problem selection is deterministic
# and traceable, not left entirely to the model to infer from a skill list.
_CODING_TOPICS: dict[str, str] = {
    "algorithms": "arrays/strings, hashing, two pointers, sliding window",
    "data_structures": "trees, graphs, heaps, linked structures",
    "concurrency": "locking, async/await, race conditions, backpressure",
    "systems": "caching, rate limiting, queues, idempotency, pagination",
    "sql": "query design, indexing, N+1s, transactions",
}

def _select_coding_topic(tech_stack: list[str], seniority: str) -> tuple[str, int]:
    """Deterministic topic + difficulty (1-5) pick from the job's stack + level.

    Pure and testable — no LLM call. Keeps 'why this problem' explainable
    instead of an opaque model choice.
    """
    stack_lower = {t.lower() for t in tech_stack}
    if stack_lower & {"kafka", "redis", "kubernetes", "grpc", "rabbitmq"}:
        topic = "systems"
    elif stack_lower & {"postgresql", "mysql", "sql"}:
        topic = "sql"
    elif stack_lower & {"go", "rust", "erlang", "elixir"}:
        topic = "concurrency"
    else:
        topic = "algorithms"
    difficulty = {
        "intern": 1, "junior": 2, "mid": 3, "senior": 4, "staff": 4, "principal": 5,
    }.get(seniority, 3)
    return topic, difficulty


def coding_round_prompts(
    candidate: CandidateProfile, job: JobSpec, gap: GapAnalysis,
    language_mode: LanguageMode,
) -> tuple[str, str]:
    topic, difficulty = _select_coding_topic(job.tech_stack, job.seniority)
    system = (
        "You are a senior interviewer designing the CODING round: ONE spoken, "
        "think-aloud problem — there is no code editor, the candidate talks "
        "through their approach out loud, like a phone screen.\n"
        f"- Ground the problem in the '{topic}' topic ({_CODING_TOPICS[topic]}), "
        f"grounded in the job's actual tech_stack, not a generic LeetCode prompt.\n"
        f"- Target difficulty {difficulty}/5 for a {job.seniority} candidate.\n"
        "- If gap.probe_targets names a specific weak technical area related to "
        f"'{topic}', prefer probing it directly.\n"
        "- rubric: exactly the reasoning steps you expect out loud (e.g. "
        "'clarifies constraints', 'names the tradeoff', 'handles the edge case') "
        "— NOT a generic 'correct syntax' criterion, since there is no editor.\n"
        "- followups: write EXACTLY ONE entry — the single hint to give if the "
        "candidate stalls (a nudge, not the answer). The live interviewer will "
        "use this verbatim instead of improvising, so make it concrete.\n"
        "Respond ONLY with the requested schema (one question)."
    )
    user = (
        f"ROLE: {job.title} ({job.seniority})\n"
        f"TECH STACK: {', '.join(job.tech_stack)}\n"
        f"CANDIDATE SKILLS: {', '.join(candidate.skills)}\n"
        f"RELEVANT GAP: {', '.join(g for g in gap.probe_targets)}"
    )
    return system, user
```

**(b) Inputs** — `job.tech_stack` + `job.seniority` (deterministic topic/difficulty
pick), `candidate.skills`, `gap.probe_targets`.

**(c) Competency mapping** — `target_competency` should be set to the chosen
topic (e.g. `"concurrency"` or `"systems_design"`) rather than left to drift;
`post/prompts.py:evaluate_answer_prompts()` needs no change — it already
scores whatever `target_competency` string it's given.

**Live-side change this enables:** `live/handoffs.py`'s
`_CODING_INSTRUCTIONS` can then say *"if they stall, use the question's
followups[0] verbatim as your one hint"* instead of leaving the hint entirely
improvised — same one-hint-if-stalled behavior, but reproducible and testable
offline against the mock adapter, matching how `save_answer` /
`get_next_question` already keep other turn behavior deterministic.

---

### 5.3 Behavioral / STAR round

**Status: not implemented as prep-time logic.** The "I not we" framing exists
only as a live instruction (`live/handoffs.py:30-35`):

```python
_BEHAVIORAL_INSTRUCTIONS = (
    "You are now running the BEHAVIORAL round. Ask one STAR-style question at a "
    "time about real past experience. Listen, then ask exactly one probing "
    "follow-up for specifics (the 'I' not the 'we'). Then call save_answer and "
    "get_next_question. Warm, concise, never leading."
)
```

Today's plan-time behavioral questions are produced by the same generic loop
as everything else (§3) — `followups` gets "at least one followup probe," with
no guarantee it's actually an individual-contribution probe rather than, say,
a generic "what would you do differently?" The live model is trusted to
invent an on-the-spot "I not we" follow-up every time, which is neither
reviewable during prep nor consistent across runs.

**(a) Proposed prompt** (draft — same schema; `followups[0]` becomes the
reserved individual-contribution probe, mirroring the coding round's use of
`followups[0]` as the reserved hint):

```python
def behavioral_round_prompts(
    candidate: CandidateProfile, job: JobSpec, gap: GapAnalysis,
    language_mode: LanguageMode,
) -> tuple[str, str]:
    system = (
        "You are a senior interviewer designing the BEHAVIORAL round: 2-3 "
        "STAR-style questions, one story at a time ('Tell me about a time "
        "you...'), each targeting a specific competency from the gap analysis "
        "— prefer gap.gaps and gap.probe_targets that are about judgment, "
        "collaboration, ownership, or leadership rather than raw technical "
        "skill (those belong in the general/coding rounds).\n"
        "- followups: write EXACTLY ONE entry per question — a probe for the "
        "candidate's OWN specific decision or action, not the team's ('What did "
        "YOU personally decide/say/do, not what the team did?'). The live "
        "interviewer asks this verbatim as its one follow-up.\n"
        "- rubric: score for concreteness of the candidate's individual role, "
        "not just outcome (e.g. 'names their own specific contribution', "
        "'explains the reasoning behind their choice').\n"
        "Respond ONLY with the requested schema."
    )
    user = (
        f"ROLE: {job.title} ({job.seniority}) at {job.company_name}\n"
        f"CANDIDATE: {candidate.headline}; achievements: "
        f"{'; '.join(candidate.achievements)}\n\n"
        f"BEHAVIORAL GAPS TO PROBE:\n- gaps: {', '.join(gap.gaps)}\n"
        f"- probe_targets: {', '.join(gap.probe_targets)}"
    )
    return system, user
```

**(b) Inputs** — `candidate.achievements` (real stories to ground questions
in), `job.title`/`seniority`, `gap.gaps` + `gap.probe_targets` filtered to
non-technical/leadership-flavored items.

**(c) Competency mapping** — same `target_competency` mechanism; the
recommendation is to steer behavioral questions toward *soft*/leadership
competencies (e.g. `"technical_leadership"`, `"ownership"`,
`"cross_team_influence"` — see the sample in
[`packages/shared/src/samples.ts:135`](../packages/shared/src/samples.ts),
which already does exactly this: `q1` is `section: "behavioral"` with
`target_competency: "technical_leadership"`), leaving technical competencies
to the general/coding rounds.

---

## 6. What it would take to wire this in (no new contracts)

The only structural change is in `apps/agent/src/proven_hire_agent/prep/`:

1. **`prep/prompts.py`** — replace `question_planner_prompts()` with the
   three builders above (§5.1-5.3).
2. **`prep/nodes.py`** — replace the single `question_planner` node with
   three calls (can run concurrently, same as `cv_analysis` /
   `jd_analysis` / `company_research` already do) that each return
   `list[PlannedQuestion]`, then a small assembly step builds the final
   `QuestionPlan` (`sections_order`, fixed `time_budget_min`, `language_mode`
   pinned from the request — all of which the node already does today at
   `nodes.py:301-303`).
   - `complete_json()` needs a `BaseModel` schema per call
     (`core/adapters/base.py:30`). `PlannedQuestion` already exists as one;
     the three calls need a *local*, non-exported wrapper like
     `class _RoundQuestions(BaseModel): questions: list[PlannedQuestion]`
     scoped to `prep/nodes.py` — it never crosses the TS/Pydantic boundary
     (nothing in `packages/shared` needs to know about it) since the node
     unwraps it into the existing `QuestionPlan` before returning state.
3. **`prep/graph.py`** — three new nodes fan out from `gap_matching` (same
   pattern as the existing `cv_analysis`/`jd_analysis`/`company_research`
   fan-out) and join into a trivial `assemble_plan` node before `END`.
4. **`live/handoffs.py`** — `_CODING_INSTRUCTIONS` and
   `_BEHAVIORAL_INSTRUCTIONS` get one added sentence each, telling the model
   to use `followups[0]` verbatim for the reserved hint / individual-
   contribution probe instead of improvising it.

This is additive prompt/orchestration work — `QuestionPlanSchema`,
`PlannedQuestionSchema`, `InterviewContext`, and `ScoreCard` in
`packages/shared` are untouched, so nothing on the `apps/web` side or the
TS↔Pydantic parity test (`apps/agent/tests/test_parity.py`) needs to change.

Trade-off worth flagging: this turns one LLM call into three (prep phase can
afford it — §"prep/live/post split" in `ARCHITECTURE.md` explicitly says prep
should favor quality over speed/cost) but does add latency and token cost to
every prep run. If that's not wanted, the lighter-touch alternative is to
keep one call but paste the three system-prompt blocks above into one prompt
as clearly labeled sections ("GENERAL ROUND RULES:", "CODING ROUND RULES:",
"BEHAVIORAL ROUND RULES:") — same content, one round-trip, less separable and
harder to unit-test per round, but keeps the number of LLM calls in prep
unchanged.

## 7. Summary

| Round | Selection logic today | Gap | Draft proposed |
|---|---|---|---|
| General | Real: `target_competency` prioritises `gap.probe_targets`/`missing_skills` | Folded into one monolithic prompt with the other two rounds; no hard quota | §5.1 — same mechanism, made a hard constraint, split into its own call |
| Coding | None — same generic rising-difficulty rule as every section; hint is improvised live, not planned | No topic taxonomy, no stack-aware difficulty, no reproducible hint | §5.2 — deterministic topic/difficulty pre-step + reserved `followups[0]` as the one hint |
| Behavioral/STAR | None — generic followup rule; "I not we" only exists as a live instruction | No plan-time individual-contribution probe; not grounded in `candidate.achievements` | §5.3 — STAR-shaped questions grounded in real achievements + reserved `followups[0]` as the individual-contribution probe |
