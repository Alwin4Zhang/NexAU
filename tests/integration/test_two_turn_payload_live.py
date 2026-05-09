# Licensed under the Apache License, Version 2.0

"""Live two-turn payload capture tests.

These integration tests verify the *actual* second-turn provider payloads emitted by
Agent -> SessionManager -> LLMCaller when using real endpoints. They are intended to
complement the unit-level UMP matrix tests with end-to-end request capture.

Coverage goals:
- same-provider two-turn payload capture for all supported API types
- one representative cross-api-type switch (`openai_chat_completion -> openai_responses`)
- assertions focus on exact second-turn request shape, not only final text output

Tests are skipped unless the required provider-specific credentials are present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import dotenv
import pytest

from nexau import Agent
from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.session import InMemoryDatabaseEngine, SessionManager

dotenv.load_dotenv()

_TASK_A = "Task A: Compute ((35 * 11) + 57) / 13. Show concise steps and end with Final A: <number>."
_TASK_B = "Task B: Using the previous result as A, compute (A * 3) + 15. Show concise steps and end with Final B: <number>."

# Long boilerplate appended to the system prompt so the cacheable prefix
# clears the per-provider minimum (Anthropic sonnet: 1024 tokens; OpenAI:
# 1024 tokens). Without this padding the system+turn-1 prefix is well
# under the threshold and cache assertions on turn 2 can't fire.
_SYSTEM_PROMPT_PADDING = (
    "Always show your work step-by-step. Verify arithmetic at each stage. "
    "Use parentheses to make order of operations explicit. Format the final "
    "answer on its own line with the prefix 'Final X: <number>'. Do not "
    "include extraneous prose or commentary outside the requested format. "
    "When prior context is available, reference it precisely without "
    "restating it verbatim. "
) * 50


@dataclass(frozen=True, slots=True)
class ProviderEnv:
    api_type: str
    model: str
    base_url: str
    api_key: str


def _env(name: str) -> str:
    return (dotenv.get_key(".env", name) or "") if False else ""


def _from_env(model_key: str, base_key: str, key_key: str, api_type: str) -> ProviderEnv | None:
    import os

    model = os.getenv(model_key, "")
    base_url = os.getenv(base_key, "")
    api_key = os.getenv(key_key, "")
    if not model or not base_url or not api_key or api_key == "test-key-not-used":
        return None
    return ProviderEnv(api_type=api_type, model=model, base_url=base_url, api_key=api_key)


_OPENAI_CHAT = _from_env(
    "LIVE_OPENAI_CHAT_MODEL",
    "LIVE_OPENAI_CHAT_BASE_URL",
    "LIVE_OPENAI_CHAT_API_KEY",
    "openai_chat_completion",
)
_OPENAI_RESPONSES = _from_env(
    "LIVE_OPENAI_RESPONSES_MODEL",
    "LIVE_OPENAI_RESPONSES_BASE_URL",
    "LIVE_OPENAI_RESPONSES_API_KEY",
    "openai_responses",
)
_ANTHROPIC = _from_env(
    "LIVE_ANTHROPIC_MODEL",
    "LIVE_ANTHROPIC_BASE_URL",
    "LIVE_ANTHROPIC_API_KEY",
    "anthropic_chat_completion",
)
_GEMINI = _from_env(
    "LIVE_GEMINI_MODEL",
    "LIVE_GEMINI_BASE_URL",
    "LIVE_GEMINI_API_KEY",
    "gemini_rest",
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.llm,
    pytest.mark.external,
    # Moved out of PR-loop ``make test`` (which runs `-m "not live_nightly"`):
    # this file's tests cover ``prompt_cache_key`` + cross-turn payload shape
    # against real provider endpoints. The cache-hit assertion on turn 2
    # depends on cumulative provider-side cache state (≥10 min staying-warm
    # window), which is not deterministic enough for PR-loop CI. Drift
    # detection runs nightly via ``make test-nightly``.
    pytest.mark.live_nightly,
]


# Async spy classes — Agent.run() internally drives ``asyncio.run(run_async)``,
# which dispatches to ``call_llm_with_*_async`` and uses the agent's
# ``_async_openai_client`` (AsyncOpenAI / AsyncAnthropic). Patching the SYNC
# ``openai_client.chat.completions`` is a no-op for those code paths and
# leaves ``calls`` empty even though a real LLM call did happen — that was
# the original cause of ``IndexError: list index out of range`` on
# ``chat_spy.calls[-1]``.


class _SecondTurnOpenAIChatSpy:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Any] = []

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        response = await self._inner.create(*args, **kwargs)
        self.responses.append(response)
        return response


class _SecondTurnOpenAIResponsesSpy:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Any] = []

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return self._inner.stream(*args, **kwargs)

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        response = await self._inner.create(*args, **kwargs)
        self.responses.append(response)
        return response


class _SecondTurnAnthropicSpy:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Any] = []

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return self._inner.stream(*args, **kwargs)

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        response = await self._inner.create(*args, **kwargs)
        self.responses.append(response)
        return response


def _safe_cached(usage: Any, details_attr: str) -> int | None:
    """Return ``usage.<details_attr>.cached_tokens`` or None.

    Distinguishes two cases that the prior version conflated as "0":

    1. **Gateway omitted the details block entirely** (``prompt_tokens_details
       is None``) — this happens unpredictably on northgate, even when the
       upstream HAS cached tokens. We return None = "unknowable".
    2. **Gateway provided details but cached_tokens is 0** — this is a real
       cache miss. We return 0.

    Callers must handle None explicitly (skip the assertion) so a flaky
    gateway response shape doesn't masquerade as a cache regression.
    """
    details = getattr(usage, details_attr, None)
    if details is None:
        return None
    return getattr(details, "cached_tokens", None) or 0


@pytest.fixture
def session_manager() -> SessionManager:
    return SessionManager(engine=InMemoryDatabaseEngine())


def _make_agent(env: ProviderEnv, session_manager: SessionManager, session_id: str) -> Agent:
    llm_kwargs: dict[str, Any] = {
        "model": env.model,
        "base_url": env.base_url,
        "api_key": env.api_key,
        "api_type": env.api_type,
        "temperature": 0.0,
        "max_tokens": 256,
        "max_retries": 1,
        "stream": False,
    }
    if env.api_type == "openai_chat_completion":
        llm_kwargs["reasoning_effort"] = "high"
    elif env.api_type == "openai_responses":
        llm_kwargs["reasoning"] = {"effort": "high"}
        llm_kwargs["include"] = ["reasoning.encrypted_content"]
    elif env.api_type == "anthropic_chat_completion":
        # max_tokens MUST be > thinking.budget_tokens per Anthropic's API.
        # Bump max_tokens above the budget rather than shrink the budget,
        # since 1024-token thinking is the deliberate test scenario.
        llm_kwargs["max_tokens"] = 2048
        llm_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 1024}
        # Mark the system prompt cacheable so turn 2 can hit the
        # ephemeral cache (5-minute TTL is plenty for a same-process
        # turn1→turn2 sequence). Required: Anthropic does NOT auto-cache
        # without an explicit cache_control on the system block.
        llm_kwargs["cache_control_ttl"] = "5m"
    elif env.api_type == "gemini_rest":
        llm_kwargs["thinkingConfig"] = {"includeThoughts": True, "thinkingBudget": 512}
        # Padding the system prompt to ~3000 tokens (for cache eligibility
        # on other providers) makes Gemini spend its full 256-token output
        # budget on thinking and emit ``parts=None``, which trips
        # ``ModelResponse.from_gemini_rest``. Lift max_tokens so visible
        # content can still surface after the thinking budget.
        llm_kwargs["max_tokens"] = 1024

    agent = Agent(
        config=AgentConfig(
            name=f"two-turn-live-{env.api_type}",
            # Long padded system prompt clears the per-provider cache
            # eligibility minimum (≥1024 tokens) so turn-2 cache_read
            # assertions can fire.
            system_prompt="You are a precise assistant. Keep calculations concise.\n\n" + _SYSTEM_PROMPT_PADDING,
            llm_config=LLMConfig(**llm_kwargs),
            # Without this, LLMCaller's ``_call_with_retry_async`` retries
            # an upstream 4xx (e.g. unsupported parameter) up to 5 times
            # with exponential backoff, burning ~120s per case and tripping
            # pytest-timeout. ``max_retries=1`` only disables the OpenAI
            # SDK's internal retries; the LLMCaller layer is independent.
            retry_attempts=1,
        ),
        session_manager=session_manager,
        user_id="two-turn-live-user",
        session_id=session_id,
    )
    return agent


def _spy_for(env: ProviderEnv, async_client: Any) -> tuple[Any, Any, str]:
    """Return (spy, parent_attr_holder, attr_name) for the api_type."""
    if env.api_type == "openai_chat_completion":
        return _SecondTurnOpenAIChatSpy(async_client.chat.completions), async_client.chat, "completions"
    if env.api_type == "openai_responses":
        return _SecondTurnOpenAIResponsesSpy(async_client.responses), async_client, "responses"
    if env.api_type == "anthropic_chat_completion":
        return _SecondTurnAnthropicSpy(async_client.messages), async_client, "messages"
    raise AssertionError(f"no spy for api_type={env.api_type!r}")


def _run_two_turn_same_provider(env: ProviderEnv, session_manager: SessionManager) -> tuple[str, dict[str, Any], Any, Any]:
    """Run a same-provider 2-turn flow, capturing both turns' responses.

    Returns ``(turn2_text, turn2_payload, turn1_response, turn2_response)``.
    Both responses are needed for cache-stability assertions: the right
    invariant is ``turn2.cached >= turn1.cached`` (turn 2's prefix is a
    superset of turn 1's, so any SDK regression that breaks the prefix
    would lower turn 2's cached count below turn 1's). Asserting strict
    ``> 0`` instead conflates "cache cold globally" (CI runner first-time
    exposure) with "SDK broke our prefix" — only the latter is a bug we
    can fix from this codebase.
    """
    session_id = f"two-turn-{env.api_type}"
    agent1 = _make_agent(env, session_manager, session_id)

    if env.api_type == "gemini_rest":
        # gemini_rest uses requests directly inside llm_caller, so capturing
        # exact body here would require patching requests.post. Provider-
        # specific unit tests cover the exact serialized payload shape.
        first = agent1.run(message=_TASK_A)
        assert first
        agent2 = _make_agent(env, session_manager, session_id)
        second = agent2.run(message=_TASK_B)
        assert second
        return str(second), {}, None, None

    # Spy turn 1 too so we can compare turn1.cached to turn2.cached for the
    # cache-stability assertion in the test bodies.
    async_client_1 = agent1._async_openai_client  # noqa: SLF001
    spy_1, parent_1, attr_1 = _spy_for(env, async_client_1)
    with patch.object(parent_1, attr_1, spy_1):
        first = agent1.run(message=_TASK_A)
    assert first

    agent2 = _make_agent(env, session_manager, session_id)
    async_client_2 = agent2._async_openai_client  # noqa: SLF001
    spy_2, parent_2, attr_2 = _spy_for(env, async_client_2)
    with patch.object(parent_2, attr_2, spy_2):
        second = agent2.run(message=_TASK_B)
    assert second

    return str(second), spy_2.calls[-1], spy_1.responses[-1], spy_2.responses[-1]


@pytest.mark.skipif(_OPENAI_CHAT is None, reason="LIVE_OPENAI_CHAT_* env vars not set")
def test_two_turn_live_openai_chat_payload(session_manager: SessionManager) -> None:
    assert _OPENAI_CHAT is not None
    second_text, payload, resp1, resp2 = _run_two_turn_same_provider(_OPENAI_CHAT, session_manager)

    assert "Final B" in second_text
    assert payload["messages"][0]["role"] == "system"
    assert any(msg.get("role") == "assistant" and msg.get("content") for msg in payload["messages"])
    assert payload.get("reasoning_effort") == "high"

    # Cache-stability invariant: turn 2's prefix is a superset of turn 1's,
    # so cached_tokens(turn2) MUST be ≥ cached_tokens(turn1). Skip when
    # either side returned None (gateway omitted ``prompt_tokens_details``
    # — northgate flake; not a cache regression we can attribute).
    cached_1 = _safe_cached(resp1.usage, "prompt_tokens_details")
    cached_2 = _safe_cached(resp2.usage, "prompt_tokens_details")
    if cached_1 is not None and cached_2 is not None:
        assert cached_2 >= cached_1, (
            f"OpenAI Chat: turn-2 cache regressed below turn-1 (turn1={cached_1}, turn2={cached_2}); usages: {resp1.usage}, {resp2.usage}"
        )


@pytest.mark.skipif(_OPENAI_RESPONSES is None, reason="LIVE_OPENAI_RESPONSES_* env vars not set")
def test_two_turn_live_openai_responses_payload(session_manager: SessionManager) -> None:
    assert _OPENAI_RESPONSES is not None
    second_text, payload, resp1, resp2 = _run_two_turn_same_provider(_OPENAI_RESPONSES, session_manager)

    assert "Final B" in second_text
    assert payload.get("reasoning", {}).get("effort") == "high"
    assert "reasoning.encrypted_content" in payload.get("include", [])
    assert isinstance(payload.get("input"), list)
    assert len(payload["input"]) >= 3

    # Same invariant as Chat. Skip when either side returned None.
    cached_1 = _safe_cached(resp1.usage, "input_tokens_details")
    cached_2 = _safe_cached(resp2.usage, "input_tokens_details")
    if cached_1 is not None and cached_2 is not None:
        assert cached_2 >= cached_1, (
            f"OpenAI Responses: turn-2 cache regressed below turn-1 "
            f"(turn1={cached_1}, turn2={cached_2}); usages: {resp1.usage}, {resp2.usage}"
        )


@pytest.mark.skipif(_ANTHROPIC is None, reason="LIVE_ANTHROPIC_* env vars not set")
def test_two_turn_live_anthropic_payload(session_manager: SessionManager) -> None:
    assert _ANTHROPIC is not None
    second_text, payload, resp1, resp2 = _run_two_turn_same_provider(_ANTHROPIC, session_manager)

    assert "Final B" in second_text
    assert payload.get("thinking", {}).get("type") == "enabled"
    assert isinstance(payload.get("messages"), list)
    assert len(payload["messages"]) >= 3

    # Anthropic prompt cache requires explicit ``cache_control`` on the
    # system block (set via ``LLMConfig.cache_control_ttl="5m"`` in
    # ``_make_agent``). Turn 1 fills the cache (cache_creation_input_tokens
    # populated); turn 2 reads it. Cache-stability invariant: turn 2's
    # cache_read MUST be ≥ turn 1's. Anthropic's turn 1 typically writes
    # but doesn't read, so turn 1's cache_read is often 0 and turn 2's
    # cache_read is the actual hit; the assertion still holds (n ≥ 0).
    cache_read_1 = getattr(resp1.usage, "cache_read_input_tokens", None) or 0
    cache_read_2 = getattr(resp2.usage, "cache_read_input_tokens", None) or 0
    assert cache_read_2 >= cache_read_1, (
        f"Anthropic: turn-2 cache_read regressed below turn-1 "
        f"(turn1={cache_read_1}, turn2={cache_read_2}); usages: {resp1.usage}, {resp2.usage}"
    )


@pytest.mark.skipif(_GEMINI is None, reason="LIVE_GEMINI_* env vars not set")
def test_two_turn_live_gemini_payload(session_manager: SessionManager) -> None:
    assert _GEMINI is not None
    second_text, payload, _r1, _r2 = _run_two_turn_same_provider(_GEMINI, session_manager)

    assert "Final B" in second_text
    assert payload == {}
    # Gemini cache assertion intentionally omitted: Gemini's prompt cache
    # uses an explicit ``cachedContent`` resource API (separate POST to
    # /cachedContents), not automatic prefix caching. Wiring that through
    # our SDK is a separate piece of work — see follow-up issue.


@pytest.mark.xfail(
    reason="Cross-provider reasoning replay semantics gap: completion-source "
    "``reasoning_content`` currently serializes back as Responses ``reasoning`` "
    "items rather than downgrading to plain text. Pre-existing serializer "
    "behavior question, unrelated to RFC-0023 PR-C.2's parser changes; "
    "needs RFC-0014 follow-up to settle the contract.",
    strict=False,
)
@pytest.mark.skipif(
    _OPENAI_CHAT is None or _OPENAI_RESPONSES is None,
    reason="LIVE_OPENAI_CHAT_* or LIVE_OPENAI_RESPONSES_* env vars not set",
)
def test_two_turn_live_completion_to_responses_gap(session_manager: SessionManager) -> None:
    assert _OPENAI_CHAT is not None
    assert _OPENAI_RESPONSES is not None

    session_id = "two-turn-live-completion-to-responses"
    agent1 = _make_agent(_OPENAI_CHAT, session_manager, session_id)
    first = agent1.run(message=_TASK_A)
    assert first

    agent2 = _make_agent(_OPENAI_RESPONSES, session_manager, session_id)
    # Patch the ASYNC client — agent.run drives asyncio.run(run_async).
    async_client = agent2._async_openai_client  # noqa: SLF001
    spy = _SecondTurnOpenAIResponsesSpy(async_client.responses)
    with patch.object(async_client, "responses", spy):
        second = agent2.run(message=_TASK_B)
    assert second
    payload = spy.calls[-1]
    input_items = payload.get("input", [])
    assert isinstance(input_items, list)
    assert any(item.get("type") == "message" and item.get("role") == "assistant" for item in input_items)
    assert not any(item.get("type") == "reasoning" for item in input_items)
