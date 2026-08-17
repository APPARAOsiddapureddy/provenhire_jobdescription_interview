"""Offline tests for the pure follow-up-necessity heuristic in ``live.state``.

Same style as ``test_adaptive.py``: imports ONLY ``proven_hire_agent.live.state``
(never ``livekit``) and drives it with lightweight ``SimpleNamespace`` fakes, since
the logic is a pure function of the current question + answer text + a
self-reported "followups asked so far" count.
"""

from __future__ import annotations

from types import SimpleNamespace

from proven_hire_agent.live import state


def _q(qid: str, section: str = "technical", followups: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=qid, section=section, followups=list(followups or []))


def _ud(questions, cursor: int = 0) -> SimpleNamespace:
    plan = SimpleNamespace(questions=list(questions))
    ctx = SimpleNamespace(plan=plan, answers=[], cursor=cursor)
    return SimpleNamespace(ctx=ctx, session_id="s_test", transcript=[])


def _words(n: int) -> str:
    return " ".join("word" for _ in range(n))


def test_thin_answer_with_budget_recommends_followup() -> None:
    questions = [_q("q1", followups=["probe deeper"])]
    ud = _ud(questions)
    sig = state.evaluate_followup_need(ud, _words(3))
    assert sig.should_follow_up is True
    assert sig.thin_answer is True
    assert sig.budget_remaining == 1


def test_hedging_answer_with_budget_recommends_followup() -> None:
    questions = [_q("q1", followups=["probe deeper"])]
    ud = _ud(questions)
    # Long enough to not be "thin", but hedges throughout.
    answer = "I think maybe " + _words(30) + " I guess, sort of."
    sig = state.evaluate_followup_need(ud, answer)
    assert sig.should_follow_up is True
    assert sig.hedging_detected is True
    assert sig.thin_answer is False


def test_rich_confident_answer_recommends_moving_on() -> None:
    questions = [_q("q1", followups=["probe deeper"])]
    ud = _ud(questions)
    sig = state.evaluate_followup_need(ud, _words(100))
    assert sig.should_follow_up is False
    assert sig.hedging_detected is False
    assert sig.thin_answer is False


def test_exhausted_budget_recommends_moving_on_even_if_thin() -> None:
    questions = [_q("q1", followups=["only one probe"])]
    ud = _ud(questions)
    sig = state.evaluate_followup_need(ud, _words(2), followups_asked_so_far=1)
    assert sig.should_follow_up is False
    assert sig.budget_remaining == 0


def test_no_planned_followups_recommends_moving_on() -> None:
    questions = [_q("q1", followups=[])]
    ud = _ud(questions)
    sig = state.evaluate_followup_need(ud, _words(2))
    assert sig.should_follow_up is False
    assert sig.budget_remaining == 0


def test_past_end_of_plan_recommends_moving_on() -> None:
    questions = [_q("q1", followups=["probe"])]
    ud = _ud(questions, cursor=5)
    sig = state.evaluate_followup_need(ud, _words(2))
    assert sig.should_follow_up is False


def test_is_deterministic_and_does_not_mutate_userdata() -> None:
    questions = [_q("q1", followups=["probe deeper"])]
    ud = _ud(questions)
    first = state.evaluate_followup_need(ud, _words(3))
    second = state.evaluate_followup_need(ud, _words(3))
    assert first == second
    assert ud.ctx.cursor == 0
    assert ud.ctx.plan.questions[0].followups == ["probe deeper"]
