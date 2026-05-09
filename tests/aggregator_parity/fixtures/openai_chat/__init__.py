"""OpenAI Chat Completions fixtures for parity testing.

All fixtures are LIVE RECORDINGS — synthetic ``delta.content: list``
fixtures previously lifted from ``test_llm_streaming.py`` were removed
after live-recording the actual OpenRouter API confirmed it does NOT
emit list-shape ``delta.content``. Real OpenRouter wire format uses
``delta.content: str`` plus ``reasoning`` / ``reasoning_details``
extension fields — covered by ``recordings/openrouter_*.sse``.

The list-content shape was likely an obsolete provider-specific
variant present when ``test_llm_streaming.py`` was originally written;
the live API has since normalized to canonical OpenAI shape with
extension fields layered on top.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from openai.types.chat import ChatCompletionChunk

FixtureFn = Callable[[], list[ChatCompletionChunk] | list[dict[str, Any]]]


def _make_recording_fixture(scenario: str) -> Callable[[], list[dict[str, Any]]]:
    from tests.aggregator_parity.sse_loader import load_recording

    def loader() -> list[dict[str, Any]]:
        return load_recording("openai_chat", scenario)

    loader.__name__ = f"fixture_recording_{scenario}"
    return loader


OPENAI_CHAT_FIXTURES: list[tuple[str, FixtureFn]] = [
    # Live recordings from northgate.xiaobei.top (deepseek-v4-flash)
    ("rec_plain_text", _make_recording_fixture("plain_text")),
    ("rec_single_tool_call", _make_recording_fixture("single_tool_call")),
    # GPT-5 series — different wire shape (whole tool call in one chunk vs
    # incremental delta from deepseek)
    ("rec_gpt5_plain", _make_recording_fixture("gpt5_plain")),
    ("rec_gpt5_tool", _make_recording_fixture("gpt5_tool")),
    # Round 3: vision + multi-turn + DeepSeek thinking
    ("rec_gpt5_vision", _make_recording_fixture("gpt5_vision")),
    ("rec_gpt5_multi_turn", _make_recording_fixture("gpt5_multi_turn")),
    ("rec_deepseek_pro_thinking", _make_recording_fixture("deepseek_pro_thinking")),
    # Round 4: logprobs + refusal-via-text
    ("rec_with_logprobs", _make_recording_fixture("with_logprobs")),
    ("rec_refusal_attempt", _make_recording_fixture("refusal_attempt")),
    # Live OpenRouter recordings via openrouter.ai/api/v1 (real production
    # wire shape): adds delta.reasoning + delta.reasoning_details extension
    # fields to canonical ChatCompletionChunk instead of changing
    # delta.content type. Different shape than the lifted dict fixtures
    # above — covers both axes of divergence.
    ("rec_openrouter_gpt_oss_plain", _make_recording_fixture("openrouter_gpt_oss_plain")),
    ("rec_openrouter_gpt_oss_tool", _make_recording_fixture("openrouter_gpt_oss_tool")),
    ("rec_openrouter_glm_plain", _make_recording_fixture("openrouter_glm_plain")),
    ("rec_openrouter_nemotron_reasoning", _make_recording_fixture("openrouter_nemotron_reasoning")),
    # Vendor-truth pair sources — also run on axis 1 / 2.
    ("rec_vt_plain", _make_recording_fixture("vt_plain")),
    ("rec_vt_tool", _make_recording_fixture("vt_tool")),
    ("rec_vt_or_reasoning", _make_recording_fixture("vt_or_reasoning")),
    # Step / step-3.5-flash via api.stepfun.com — uses the BARE ``reasoning``
    # extension key (no ``_content`` suffix), distinct from DeepSeek's
    # ``reasoning_content`` and OpenRouter's structured ``reasoning_details``.
    # See case study 2026-05-09-step-3.5-flash-pathologies.md for the full
    # investigation (bisect / workaround failure matrix / decisions).
    #
    # Minimal regression set — each fixture covers one distinct code path:
    #   plain                       → bare-reasoning + content path (happy)
    #   tool_call                   → standard tool_calls path     (happy)
    #   reasoning_only_truncated    → build() reasoning-only branch (bug b)
    #   xml_tool_in_reasoning_threshold → executor _has_empty_payload  (bug c)
    #   workaround_anti_xml_*       → proves bug c is unfixable from our side
    ("rec_step35_plain", _make_recording_fixture("step35_plain")),
    ("rec_step35_tool_call", _make_recording_fixture("step35_tool_call")),
    ("rec_step35_reasoning_only_truncated", _make_recording_fixture("step35_reasoning_only_truncated")),
    ("rec_step35_xml_tool_in_reasoning_threshold", _make_recording_fixture("step35_xml_tool_in_reasoning_threshold")),
    (
        "rec_step35_workaround_anti_xml_system_prompt_still_fails",
        _make_recording_fixture("step35_workaround_anti_xml_system_prompt_still_fails"),
    ),
]
