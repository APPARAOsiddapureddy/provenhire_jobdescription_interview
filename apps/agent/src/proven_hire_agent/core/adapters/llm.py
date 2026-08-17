"""LLM adapter factory + real adapters (lazy-imported SDKs).

``get_llm(settings)`` returns the deterministic :class:`MockLLM` unless an LLM
provider is selected *and* its API key is present; otherwise it logs a warning
and falls back to the mock. Real adapters import their SDK inside methods so the
module imports cleanly with no SDK installed.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from ..logging import get_logger
from .base import LLMAdapter
from .mock import MockLLM

if TYPE_CHECKING:
    from ..config import Settings

log = get_logger(__name__)

# Per-call ceiling so a stalled provider call can never hang a pipeline forever
# (the prep graph's per-node try/except only fires once the call RETURNS).
_DEFAULT_TIMEOUT_SEC = 90.0


def _schema_prompt(system: str, schema: type) -> str:
    """Append the JSON Schema to the system prompt (provider-agnostic).

    Both Gemini's ``response_schema`` and OpenAI's strict structured outputs
    reject free-form maps like our ``LocalizedText = dict[str, str]``
    (additionalProperties), so the reliable cross-provider pattern is JSON mode
    + the schema in the prompt + Pydantic validation of the result.
    """
    schema_json = json.dumps(schema.model_json_schema())
    return (
        f"{system}\n\nReturn ONLY a single JSON value that conforms to this "
        f"JSON Schema (no markdown, no commentary):\n{schema_json}"
    )


def _loads_json(text: str) -> Any:
    """Parse JSON from an LLM response, tolerating ```json fences, stray prose, and
    *trailing* "Extra data".

    The keystone failure this fixes: the question planner has the largest schema,
    so its real Gemini response is the biggest — and a complete JSON object was
    sometimes FOLLOWED by extra content (a second object / trailing commentary).
    Plain ``json.loads`` raises ``Extra data`` on that, and the old greedy
    ``\\{.*\\}`` fallback spanned to the LAST brace (swallowing the trailing junk
    into invalid JSON), so every plan silently fell back to the generic mock —
    an interview that asks one question literally titled "mock". ``raw_decode``
    returns the FIRST complete JSON value and ignores anything after it.
    """
    import re

    t = (text or "").strip()
    # Strip reasoning blocks BEFORE looking for JSON. Local reasoning models
    # (Qwen3 via Ollama) routinely return "<think>…</think>" inline in content,
    # and the tolerant scan below takes the FIRST '{' it sees — so a brace
    # anywhere in the model's scratchpad would be decoded instead of the answer.
    # Cloud models don't emit these tags, so this is a no-op for them.
    t = re.sub(r"<(think|thinking)>.*?</\1>", "", t, flags=re.DOTALL | re.IGNORECASE).strip()
    # An unterminated block means the reply was cut off mid-thought; everything
    # after the opening tag is scratchpad, never the answer.
    t = re.sub(r"<(think|thinking)>.*\Z", "", t, flags=re.DOTALL | re.IGNORECASE).strip()
    # Strip a wrapping ```json ... ``` / ``` ... ``` markdown fence, if present.
    if t.startswith("```"):
        t = re.sub(r"^```[^\n]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t).strip()
    # Fast path: a single clean JSON value.
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # Tolerant path: decode the first complete JSON value starting at the first
    # '{' or '[', ignoring any trailing data (raw_decode is "Extra data"-safe).
    decoder = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch in "{[":
            try:
                obj, _end = decoder.raw_decode(t, i)
                return obj
            except json.JSONDecodeError:
                continue
    # Last resort: the response was likely cut off mid-object (a stalled
    # stream, a hit max-tokens ceiling) rather than genuinely malformed —
    # try closing the unbalanced brackets and parsing once more.
    repaired = _repair_truncated_json(t)
    if repaired is not None:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass
    raise json.JSONDecodeError("no JSON value found in LLM response", t, 0)


def _repair_truncated_json(t: str) -> str | None:
    """Best-effort repair for a JSON value truncated mid-object/array: track a
    bracket stack (respecting string/escape state) from the first ``{``/``[``
    and, if unbalanced at EOF, close it by appending the matching closers (and
    a closing quote if truncated mid-string).

    Returns ``None`` when there is nothing to repair (no opener found, or the
    brackets are already balanced — i.e. this text is not the "cut off before
    closing" case and a genuine parse failure is the right outcome). This does
    NOT recover a value truncated mid-token (e.g. a dangling ``"score": 4.`` or
    a trailing comma before EOF) — only a complete run of key/value pairs that
    simply never got its closing brackets.
    """
    start = next((i for i, ch in enumerate(t) if ch in "{["), None)
    if start is None:
        return None
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in t[start:]:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    if not stack:
        return None
    closing = ('"' if in_string else "") + "".join(reversed(stack))
    return t[start:] + closing


class GeminiLLM:
    """Google Gemini via ``google-genai`` (lazy import)."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    ) -> None:
        # No default model: ids retire fast, so the current id lives in ONE
        # place (Settings.gemini_model) and must be passed in explicitly.
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_sec

    def _client(self) -> Any:
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - depends on optional SDK
            raise RuntimeError(
                "google-genai is not installed; install the 'gemini' extra."
            ) from exc
        return genai.Client(api_key=self._api_key)

    async def complete_text(self, *, system: str, user: str) -> str:
        client = self._client()
        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=self._model,
                contents=user,
                config={"system_instruction": system},
            ),
            timeout=self._timeout,
        )
        return resp.text or ""

    async def complete_json(self, *, system: str, user: str, schema: type) -> Any:
        # JSON mode + schema-in-prompt (see _schema_prompt for why not
        # ``response_schema``).
        client = self._client()
        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=self._model,
                contents=user,
                config={
                    "system_instruction": _schema_prompt(system, schema),
                    "response_mime_type": "application/json",
                },
            ),
            timeout=self._timeout,
        )
        return schema.model_validate(_loads_json(resp.text or "{}"))


class OpenAILLM:
    """OpenAI — and any OpenAI-compatible server — via the ``openai`` SDK.

    ``base_url`` is what makes the local path possible: pointed at Ollama's
    ``/v1`` it drives the whole prep/post pipeline with no cloud key. See
    :class:`OllamaLLM`.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
        base_url: str | None = None,
    ) -> None:
        # No default model — see GeminiLLM.__init__; Settings.openai_model is
        # the single source of truth.
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_sec
        self._base_url = base_url

    def _client(self) -> Any:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - depends on optional SDK
            raise RuntimeError(
                "openai is not installed; install the 'openai' extra."
            ) from exc
        return AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)

    async def complete_text(self, *, system: str, user: str) -> str:
        client = self._client()
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            ),
            timeout=self._timeout,
        )
        return resp.choices[0].message.content or ""

    async def complete_json(self, *, system: str, user: str, schema: type) -> Any:
        # JSON mode + schema-in-prompt, NOT strict structured outputs: OpenAI's
        # strict mode rejects free-form maps (additionalProperties) like our
        # ``LocalizedText = dict[str, str]``, which would silently break the
        # question planner (every plan falling back to mock). Same pattern as
        # the Gemini adapter; Pydantic validates the result either way.
        client = self._client()
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _schema_prompt(system, schema)},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            ),
            timeout=self._timeout,
        )
        return schema.model_validate(_loads_json(resp.choices[0].message.content or "{}"))


class OllamaLLM(OpenAILLM):
    """A local Ollama server through its OpenAI-compatible ``/v1`` endpoint.

    Ollama needs no credential, so a non-empty placeholder key is passed (the
    SDK requires *something*). Behaviourally this differs from the cloud path in
    one way that matters: **it retries once when the response doesn't parse.**

    Why only here. Small local models are markedly worse at emitting strict JSON
    than Gemini/GPT, and each round-generation node (``prep.nodes.general_round``
    / ``coding_round`` / ``behavioral_round``) still risks the same failure mode
    on a parse miss: it silently swaps in a generic mock question set and the
    candidate sits through an interview whose questions are titled "mock". That
    exact failure has shipped before. One cheap retry with a blunter instruction
    converts most near-misses
    (a stray preamble, a truncated trailing brace) into a usable plan; the cloud
    adapters stay single-shot so their latency and cost are unchanged.
    """

    _RETRY_NUDGE = (
        "Your previous reply could not be parsed as JSON. Reply with the JSON "
        "value ONLY — no explanation, no markdown fence, no <think> block."
    )

    async def complete_json(self, *, system: str, user: str, schema: type) -> Any:
        try:
            return await super().complete_json(system=system, user=user, schema=schema)
        except Exception as exc:  # noqa: BLE001 - any parse/validation miss earns one retry
            log.warning("OllamaLLM: unparseable JSON (%s); retrying once.", exc)
            return await super().complete_json(
                system=f"{system}\n\n{self._RETRY_NUDGE}", user=user, schema=schema
            )


class FallbackLLM:
    """Wraps a primary adapter; on a ``complete_json`` failure, retries each
    configured fallback adapter once, in order, before giving up.

    Re-raises the ORIGINAL primary exception if every fallback also fails (not
    the last fallback's exception), so a caller's degrade-to-mock warning log
    still names the actually-configured provider rather than whichever
    fallback happened to fail last. ``complete_text`` is NOT covered — it goes
    straight to the primary, matching every current call site's tolerance for
    single-provider behavior on that path.
    """

    def __init__(self, primary: LLMAdapter, fallbacks: list[LLMAdapter]) -> None:
        self._primary = primary
        self._fallbacks = fallbacks

    async def complete_text(self, *, system: str, user: str) -> str:
        return await self._primary.complete_text(system=system, user=user)

    async def complete_json(self, *, system: str, user: str, schema: type) -> Any:
        try:
            return await self._primary.complete_json(system=system, user=user, schema=schema)
        except Exception as primary_exc:  # noqa: BLE001 - fall through to fallbacks below
            for fallback in self._fallbacks:
                try:
                    return await fallback.complete_json(system=system, user=user, schema=schema)
                except Exception:  # noqa: BLE001 - try the next fallback
                    continue
            log.warning(
                "FallbackLLM: primary and all %d fallback(s) failed; re-raising the "
                "primary's error.",
                len(self._fallbacks),
            )
            raise primary_exc


# Fixed priority order fallbacks are tried in, skipping whichever is primary.
# Not currently configurable — a reasonable fixed default rather than a new
# setting, since this only matters when MULTIPLE providers happen to have keys
# configured at once. Deliberately EXCLUDES "ollama": unlike gemini_api_key/
# openai_api_key (None unless the user opts in), Settings.ollama_base_url has
# a non-empty default ("http://localhost:11434/v1") even when nobody set it —
# so "base_url is present" can't tell apart "I run a local model" from "the
# field is just sitting at its default." Auto-adding it as a fallback would
# mean nearly every cloud-primary deployment silently gets a phantom fallback
# to a local server that was never actually started, wasting a connection
# attempt/timeout on every primary failure before it re-raises anyway. Ollama
# can still be the PRIMARY provider (llm_provider=ollama, unaffected by this).
_PROVIDER_FALLBACK_ORDER: tuple[str, ...] = ("gemini", "openai")


def _build_adapter(provider: str, settings: Settings) -> LLMAdapter | None:
    """Construct the adapter for ``provider`` if its config is present, else
    ``None``. Uses ``getattr`` (not direct attribute access) so probing a
    provider OTHER than the one named in ``settings.llm_provider`` can't raise
    on a minimal/test ``Settings`` stand-in that only defines the fields its
    own scenario needs.
    """
    # Model-name fallbacks below mirror core/config.py's own Settings defaults —
    # they only ever fire against a test double that omits the field; the real
    # Settings class always sets its own value, so this changes nothing there.
    timeout = getattr(settings, "llm_call_timeout_sec", _DEFAULT_TIMEOUT_SEC)
    if provider == "gemini":
        api_key = getattr(settings, "gemini_api_key", None)
        if api_key:
            model = getattr(settings, "gemini_model", "gemini-3.6-flash")
            return GeminiLLM(api_key, model, timeout)
        return None
    if provider == "openai":
        api_key = getattr(settings, "openai_api_key", None)
        if api_key:
            model = getattr(settings, "openai_model", "gpt-5.1-mini")
            return OpenAILLM(api_key, model, timeout)
        return None
    if provider in {"ollama", "vllm", "llamacpp", "lmstudio", "local"}:
        base_url = getattr(settings, "ollama_base_url", None)
        if base_url:
            return OllamaLLM(
                getattr(settings, "local_api_key", ""),
                getattr(settings, "ollama_model", "qwen3:8b"),
                timeout,
                base_url=base_url,
            )
        return None
    return None


def get_llm(settings: Settings) -> LLMAdapter:
    """Choose an LLM adapter from settings, falling back to the mock.

    When the configured provider is set up AND at least one OTHER provider
    also has its config present, the returned adapter is wrapped in a
    :class:`FallbackLLM` so a single flaky/rate-limited provider doesn't force
    every caller straight to the generic mock plan when another real provider
    is available. With only one provider configured (the common case), this
    returns the primary adapter unchanged — today's exact behavior.
    """
    provider = (settings.llm_provider or "mock").lower()
    if provider == "mock":
        return MockLLM()

    normalized = "ollama" if provider in {"vllm", "llamacpp", "lmstudio", "local"} else provider
    primary = _build_adapter(provider, settings)
    if primary is None:
        log.warning("llm_provider=%s but its config is missing; using MockLLM.", provider)
        return MockLLM()

    fallbacks = [
        adapter
        for other in _PROVIDER_FALLBACK_ORDER
        if other != normalized
        for adapter in [_build_adapter(other, settings)]
        if adapter is not None
    ]
    return FallbackLLM(primary, fallbacks) if fallbacks else primary
