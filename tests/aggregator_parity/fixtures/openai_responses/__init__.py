"""Synthetic OpenAI Responses event fixtures for parity testing."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from openai.types.responses import ResponseStreamEvent

from .from_test_llm_streaming import fixture_real_text_tool_reasoning_combined
from .text_only import fixture_text_only

FixtureFn = Callable[[], list[ResponseStreamEvent] | list[dict[str, Any]]]


def _make_recording_fixture(scenario: str) -> Callable[[], list[dict[str, Any]]]:
    from tests.aggregator_parity.sse_loader import load_recording

    def loader() -> list[dict[str, Any]]:
        return load_recording("openai_responses", scenario)

    loader.__name__ = f"fixture_recording_{scenario}"
    return loader


OPENAI_RESPONSES_FIXTURES: list[tuple[str, FixtureFn]] = [
    ("text_only", fixture_text_only),
    ("real_text_tool_reasoning_combined", fixture_real_text_tool_reasoning_combined),
    # Live recordings from northgate.xiaobei.top gateway (gpt-5.2)
    ("rec_plain_text", _make_recording_fixture("plain_text")),
    ("rec_tool_call", _make_recording_fixture("tool_call")),
    # gpt-5.4 with explicit reasoning + tool + parallel
    ("rec_gpt5_tool_with_reasoning", _make_recording_fixture("gpt5_tool_with_reasoning")),
    ("rec_gpt5_parallel_tools", _make_recording_fixture("gpt5_parallel_tools")),
    # Round 3: vision + multi-turn + variants + truncation + high reasoning
    ("rec_gpt5_vision", _make_recording_fixture("gpt5_vision")),
    ("rec_gpt5_tool_result_followup", _make_recording_fixture("gpt5_tool_result_followup")),
    ("rec_gpt55_plain", _make_recording_fixture("gpt55_plain")),
    ("rec_gpt5_with_instructions", _make_recording_fixture("gpt5_with_instructions")),
    ("rec_gpt5_truncated", _make_recording_fixture("gpt5_truncated")),
    ("rec_gpt5_high_reasoning", _make_recording_fixture("gpt5_high_reasoning")),
    # Round 4: refusal-via-text (text refusal, not structured refusal block)
    ("rec_refusal_attempt", _make_recording_fixture("refusal_attempt")),
    # Vendor-truth pair sources — also run on axis 1 / 2.
    ("rec_vt_plain", _make_recording_fixture("vt_plain")),
    ("rec_vt_tool", _make_recording_fixture("vt_tool")),
    ("rec_vt_reasoning", _make_recording_fixture("vt_reasoning")),
]
