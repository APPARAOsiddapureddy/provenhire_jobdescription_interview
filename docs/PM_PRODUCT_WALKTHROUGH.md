# Proven Hire — How the Interview Product Works
*A product-manager-level walkthrough (no code required)*

## 1. The one-liner

Upload a CV and a job description → talk out loud to an AI interviewer with a
stylized avatar → get a scored report and a personalized study plan for what
you missed. It's a closed loop: **prep → interview → feedback → prep again.**

No sign-in is required to try it. There's a one-click "Quick demo" on the
setup screen that fills in a sample CV + JD so a prospect can experience the
whole loop before uploading anything of their own — this matters for
activation/conversion funnels since there's zero friction to a first "aha".

---

## 2. The user journey (what the candidate actually experiences)

| Step | Screen | What the user does | What happens behind the scenes |
|---|---|---|---|
| **1. Setup** | `/setup` | Uploads a CV + pastes/uploads a job description. Picks a language. | Nothing heavy yet — just capture. |
| **2. Prep** | `/prep` | Waits a short moment (this is the "thinking" step). | The system reads the CV and JD, researches the target company, figures out the candidate's skill gaps vs. the role, and builds a custom **question plan** — not a generic question bank. |
| **3. Live interview** | `/interview` | Talks out loud, in real time, to an AI interviewer (voice + avatar). Can be asked general questions, a live coding round, and a behavioral/STAR round. | The interviewer works strictly from the pre-built plan. It asks, listens, records the answer, and moves to the next question. It deliberately does **no research or scoring while the candidate is talking** — that's saved for after, so the conversation stays snappy. |
| **4. Report** | `/report` | Gets a scored report: overall score, strengths, weaknesses, a model ("ideal") answer per question, and a language/communication assessment. | The full transcript is graded against the plan's rubric, competency by competency. |
| **5. Coach (loop back)** | `/prep` (coach) | Gets a targeted study plan for the specific weak areas found in the report. | The weak spots feed directly into the next prep cycle, closing the loop. |

---

## 3. The "why" behind the design: prep / live / post

This is the single most important product decision in the system, so it's
worth explaining in plain terms:

- **Before the call (Prep):** the system is allowed to think slowly and
  expensively — read documents, search the web for company info, reason about
  skill gaps. Quality over speed.
- **During the call (Live):** the AI is only allowed to do one fast, simple
  job — ask the next planned question, listen, save the answer. No research,
  no scoring, no database lookups mid-conversation. This is what keeps the
  live conversation feeling like a real-time conversation instead of a
  laggy chatbot.
- **After the call (Post):** back to slow-and-thorough — grading, coaching
  feedback, writing the report.

**Product implication:** interview *quality* (how smart the questions are)
and interview *feel* (how natural the conversation is) are decoupled and can
be improved independently. You can make the prep researcher smarter without
ever risking the live call feeling laggy.

---

## 4. What "the interview" actually contains

The live session isn't one monolithic Q&A — it hands off between three
specialist personas, seamlessly, without the candidate re-explaining
themselves:

1. **General interviewer** — asks the core planned questions from the
   question plan, tailored to the CV + JD.
2. **Coding round** — a hands-on technical problem tied to the candidate's
   stated stack. Important nuance: this is a **spoken, "think-aloud"
   coding round**, not a code editor / IDE. The candidate talks through
   their approach the way they would in a real whiteboard/phone-screen
   interview; the AI nudges with one hint if they stall.
3. **Behavioral / STAR round** — one story-based question at a time (e.g.
   "tell me about a time..."), with exactly one probing follow-up pushing
   for the candidate's individual contribution, not the team's.

All three share the same underlying tools to record an answer and advance to
the next question, so the transition feels like one continuous interviewer,
not three bots stitched together.

---

## 5. What the candidate gets at the end (the Report)

The output is a structured scorecard, not just a number:

- **Overall score**
- **Per-competency scores** (e.g., "System Design: 6/10") with the evidence
  behind each score and a mastery level
- **Strengths** and **weaknesses**, called out explicitly
- **Weak competencies** — the specific short list that feeds the next coaching
  cycle
- **Model answers** — what a strong answer to each question would have
  sounded like
- **Language/communication report** — fluency, filler-word count, clarity,
  pronunciation notes, and code-switching notes (relevant for multilingual
  candidates)
- **Next steps** — concrete study recommendations
- **Coverage %** — if the candidate ran out of time or left early, the report
  distinguishes "didn't get to it" from "answered it poorly," so a short
  session doesn't unfairly tank the score

---

## 6. Product-relevant flexibility (things worth knowing for GTM/pricing)

- **No AI vendor lock-in.** Every AI stage — speech-to-text, the text-to-speech
  voice, and the reasoning LLM — is swappable independently via config. This
  means unit economics can be tuned per deployment (cheap defaults vs.
  premium voice, etc.) without an engineering rewrite.
- **Fully local / offline mode exists.** All models (speech + reasoning) can
  run on the customer's own machine with zero API keys and zero data leaving
  their device. This is a real privacy/enterprise selling point — useful for
  security-conscious customers (e.g., candidates prepping with confidential
  internal JDs, or enterprises who want on-prem).
  Positioned similarly to "mock adapters run with **no API keys at all**" —
  so demos, CI, and evaluation can run for free before any vendor billing
  decision is made.
- **Multilingual by design.** English and Vietnamese ship today; the
  architecture treats language as a pluggable "pack," so adding a new
  interview language is a content/localization task, not a re-architecture.
- **Self-hosted, anonymous by default.** Setup, the live interview, and the
  report all work without an account. Login exists but isn't a gate to try
  the product — worth knowing when designing the funnel/paywall.

---

## 7. The three moving services (simplified, non-technical)

Think of the product as three cooperating services:

1. **The web app** — everything the candidate sees and clicks: upload, the
   live room, the report, the prep coach. It owns the account/login/token
   layer and knows nothing about which AI vendor is being used underneath.
2. **The agent** — the actual "brain": runs the prep research, the live voice
   conversation, and the post-call scoring.
3. **The knowledge base** — a side service that lets the agent look up and
   ground company research (so company facts in prep are retrieved with
   citations rather than freely hallucinated).

A shared "contract" layer keeps the web app and the agent's brain speaking
the exact same data language (candidate profile, question plan, scorecard),
so the two can be built and shipped somewhat independently without their
data models drifting apart.

---

## 8. Status / roadmap note

The build is tracked as 13 discrete work packages (WP-0 through WP-13) via
GitHub issues on the repo — useful if you want a live view of what's shipped
vs. in progress rather than relying on this document, which is a snapshot.

---

*Source repo: `provenhire_jobdescription_interview`. This document
summarizes `README.md` and `docs/ARCHITECTURE.md` in product terms; see those
files for the engineering-level detail.*
