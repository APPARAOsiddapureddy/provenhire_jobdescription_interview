"""Offline tests for the deterministic mock adapters + local provider selection."""

import asyncio
from types import SimpleNamespace

from proven_hire_agent.core.adapters.llm import (
    FallbackLLM,
    GeminiLLM,
    OllamaLLM,
    OpenAILLM,
    get_llm,
)
from proven_hire_agent.core.adapters.mock import (
    MockEmbeddings,
    MockLLM,
    MockSearch,
    build_mock,
)
from proven_hire_agent.shared_models import (
    InterviewContext,
    QuestionPlan,
    ScoreCard,
)


def _run(coro):
    return asyncio.run(coro)


def test_build_mock_question_plan_is_valid() -> None:
    plan = build_mock(QuestionPlan)
    assert isinstance(plan, QuestionPlan)
    assert len(plan.questions) >= 1
    q = plan.questions[0]
    assert q.text["en"] == "mock"
    assert 1 <= q.difficulty <= 5
    assert len(q.rubric) >= 1
    assert len(q.followups) >= 1


def test_mock_llm_complete_json_schema_valid() -> None:
    plan = _run(MockLLM().complete_json(system="s", user="u", schema=QuestionPlan))
    assert isinstance(plan, QuestionPlan)

    ctx = _run(MockLLM().complete_json(system="s", user="u", schema=InterviewContext))
    assert isinstance(ctx, InterviewContext)
    # Defaulted fields stay at their defaults.
    assert ctx.cursor == 0
    assert ctx.answers == []
    assert ctx.scorecard is None

    sc = _run(MockLLM().complete_json(system="s", user="u", schema=ScoreCard))
    assert isinstance(sc, ScoreCard)
    assert sc.language_report.summary == "mock"


def test_mock_llm_complete_json_deterministic() -> None:
    a = _run(MockLLM().complete_json(system="s", user="u", schema=QuestionPlan))
    b = _run(MockLLM().complete_json(system="s", user="u", schema=QuestionPlan))
    assert a.model_dump() == b.model_dump()


def test_mock_llm_complete_text_is_str() -> None:
    text = _run(MockLLM().complete_text(system="s", user="u"))
    assert isinstance(text, str) and text


def test_mock_search_deterministic() -> None:
    a = _run(MockSearch().search("acme payments"))
    b = _run(MockSearch().search("acme payments"))
    assert [r.model_dump() for r in a] == [r.model_dump() for r in b]
    assert len(a) >= 1


def test_mock_embeddings_deterministic_and_dim() -> None:
    a = _run(MockEmbeddings().embed(["hello", "world"]))
    b = _run(MockEmbeddings().embed(["hello", "world"]))
    assert a == b
    assert len(a) == 2
    assert all(len(v) == MockEmbeddings.DIM for v in a)
    # Different text -> different vector.
    assert a[0] != a[1]


# --- local provider selection (the offline half of the "no cloud keys" path) ---
#
# These pin the FACTORY, not the servers: constructing an adapter performs no
# network I/O, so they stay hermetic and run in plain CI (no extras needed).
# What must not regress is the contract that a misconfigured local provider
# degrades to the mock instead of raising — and, just as importantly, that a
# correctly configured one does NOT silently stay on the mock.


def _settings(**overrides):
    """Real Settings with env ignored, so a developer's .env can't sway a test."""
    base = {
        "llm_provider": "mock",
        "ollama_base_url": "http://localhost:11434/v1",
        "ollama_model": "qwen3:8b",
        "local_api_key": "local",
        "llm_call_timeout_sec": 90.0,
        "gemini_api_key": None,
        "gemini_model": "gemini-3.6-flash",
        "openai_api_key": None,
        "openai_model": "gpt-5.1-mini",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_get_llm_ollama_returns_local_adapter() -> None:
    """LLM_PROVIDER=ollama + a base URL selects the local adapter, not the mock.

    The failure this guards against is silent: a local provider that falls
    through to MockLLM still "works", and every generated question comes back
    titled "mock" (see the _loads_json docstring in core/adapters/llm.py).
    """
    llm = get_llm(_settings(llm_provider="ollama"))
    assert isinstance(llm, OllamaLLM)
    assert llm._base_url == "http://localhost:11434/v1"
    assert llm._model == "qwen3:8b"
    # No cloud credential is involved: the key is the non-empty placeholder the
    # openai SDK requires, never a real one.
    assert llm._api_key == "local"


def test_get_llm_ollama_without_base_url_falls_back_to_mock() -> None:
    llm = get_llm(_settings(llm_provider="ollama", ollama_base_url=""))
    assert isinstance(llm, MockLLM)


def test_ollama_llm_retries_once_on_unparseable_json() -> None:
    """A small local model's first reply is often unparseable; one retry saves it.

    Gemini/GPT are single-shot; this retry exists only on the local path, where
    the keystone QuestionPlan call is the one most likely to come back wrapped
    in prose or a <think> block.
    """
    calls: list[str] = []
    llm = OllamaLLM("local", "qwen3:8b", 90.0, base_url="http://x/v1")

    async def _fake(_self, *, system: str, user: str, schema: type):
        calls.append(system)
        if len(calls) == 1:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return build_mock(schema)

    # Patch the inherited OpenAI implementation the subclass delegates to.
    import proven_hire_agent.core.adapters.llm as llm_mod

    original = llm_mod.OpenAILLM.complete_json
    llm_mod.OpenAILLM.complete_json = _fake  # type: ignore[assignment]
    try:
        out = _run(llm.complete_json(system="s", user="u", schema=QuestionPlan))
    finally:
        llm_mod.OpenAILLM.complete_json = original  # type: ignore[assignment]

    assert isinstance(out, QuestionPlan)
    assert len(calls) == 2, "expected exactly one retry"
    assert "could not be parsed as JSON" in calls[1], "retry must add the nudge"
    assert "could not be parsed as JSON" not in calls[0], "first call stays clean"


# --- FallbackLLM + multi-provider get_llm() selection --------------------------


class _FakeAdapter:
    """A minimal LLMAdapter stand-in: succeeds, or raises, on command."""

    def __init__(self, *, name: str, fails: bool = False) -> None:
        self.name = name
        self.fails = fails
        self.calls = 0

    async def complete_text(self, *, system: str, user: str) -> str:
        return self.name

    async def complete_json(self, *, system: str, user: str, schema: type):
        self.calls += 1
        if self.fails:
            raise RuntimeError(f"{self.name} failed")
        return build_mock(schema)


def test_fallback_llm_uses_fallback_when_primary_fails() -> None:
    primary = _FakeAdapter(name="primary", fails=True)
    fallback = _FakeAdapter(name="fallback")
    llm = FallbackLLM(primary, [fallback])
    out = _run(llm.complete_json(system="s", user="u", schema=QuestionPlan))
    assert isinstance(out, QuestionPlan)
    assert primary.calls == 1
    assert fallback.calls == 1


def test_fallback_llm_reraises_primary_error_when_all_fail() -> None:
    primary = _FakeAdapter(name="primary", fails=True)
    fallback = _FakeAdapter(name="fallback", fails=True)
    llm = FallbackLLM(primary, [fallback])
    try:
        _run(llm.complete_json(system="s", user="u", schema=QuestionPlan))
    except RuntimeError as exc:
        assert "primary failed" in str(exc), "must re-raise the PRIMARY's error"
    else:
        raise AssertionError("expected a RuntimeError")


def test_fallback_llm_skips_fallbacks_when_primary_succeeds() -> None:
    primary = _FakeAdapter(name="primary")
    fallback = _FakeAdapter(name="fallback")
    llm = FallbackLLM(primary, [fallback])
    _run(llm.complete_json(system="s", user="u", schema=QuestionPlan))
    assert primary.calls == 1
    assert fallback.calls == 0


def test_get_llm_single_configured_provider_returns_bare_adapter() -> None:
    """Only one provider configured -> no FallbackLLM wrapping, today's behavior."""
    llm = get_llm(_settings(llm_provider="gemini", gemini_api_key="g-key"))
    assert isinstance(llm, GeminiLLM)


def test_get_llm_wraps_in_fallback_when_multiple_providers_configured() -> None:
    llm = get_llm(
        _settings(
            llm_provider="gemini",
            gemini_api_key="g-key",
            openai_api_key="o-key",
        )
    )
    assert isinstance(llm, FallbackLLM)
    assert isinstance(llm._primary, GeminiLLM)
    assert len(llm._fallbacks) == 1
    assert isinstance(llm._fallbacks[0], OpenAILLM)


def test_get_llm_fallback_order_excludes_the_primary_provider() -> None:
    """openai is primary; gemini is the only other one configured -> gemini-only
    fallback list."""
    llm = get_llm(
        _settings(
            llm_provider="openai",
            openai_api_key="o-key",
            gemini_api_key="g-key",
        )
    )
    assert isinstance(llm, FallbackLLM)
    assert isinstance(llm._primary, OpenAILLM)
    assert len(llm._fallbacks) == 1
    assert isinstance(llm._fallbacks[0], GeminiLLM)


def test_get_llm_never_auto_falls_back_to_ollama() -> None:
    """Settings.ollama_base_url has a non-empty DEFAULT even when nobody
    configured it — so "present" can't mean "the user wants Ollama as a
    fallback." A cloud primary with no other cloud key configured must return
    the bare adapter, not a phantom FallbackLLM pointed at a local server that
    was never started."""
    llm = get_llm(_settings(llm_provider="gemini", gemini_api_key="g-key"))
    assert isinstance(llm, GeminiLLM), (
        "must NOT wrap in FallbackLLM just because ollama_base_url has its "
        "default value"
    )
