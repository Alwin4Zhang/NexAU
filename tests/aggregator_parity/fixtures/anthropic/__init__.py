"""Synthetic Anthropic event fixtures for parity testing.

Each function returns a list of ``RawMessageStreamEvent`` instances that mimic
what the Anthropic SDK would yield on a real streaming call. Both Set A and
Set B aggregators consume these directly via their normal entry points.

To add a new fixture:
1. Define a new ``def fixture_xxx() -> list[RawMessageStreamEvent]`` here
2. Add ``("xxx", fixture_xxx)`` to ``ANTHROPIC_FIXTURES`` below
3. The parametrized parity test in ``test_anthropic_parity.py`` picks it up
   automatically.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from anthropic.types import RawMessageStreamEvent

from .from_test_llm_streaming import (
    fixture_real_concatenated_tool_json,
    fixture_real_eager_streaming_none_initial_fields,
    fixture_real_text_basic,
    fixture_real_thinking_delta_accumulates,
    fixture_real_thinking_delta_without_block_start,
    fixture_real_thinking_then_text,
)
from .parallel_tool_calls import fixture_parallel_tool_calls
from .plain_text import fixture_plain_text
from .thinking_then_text import fixture_thinking_then_text

# Each fixture function returns a list of either ``RawMessageStreamEvent`` SDK
# instances (synthetic fixtures) or loose dicts (fixtures lifted from
# ``test_llm_streaming.py``). The Anthropic glue normalizes loose dicts to SDK
# types before feeding Set A.
FixtureFn = Callable[[], list[RawMessageStreamEvent] | list[dict[str, Any]]]


def _make_recording_fixture(scenario: str) -> Callable[[], list[dict[str, Any]]]:
    """Wrap a recording into a fixture-fn that loads on demand."""
    from tests.aggregator_parity.sse_loader import load_recording

    def loader() -> list[dict[str, Any]]:
        return load_recording("anthropic", scenario)

    loader.__name__ = f"fixture_recording_{scenario}"
    return loader


ANTHROPIC_FIXTURES: list[tuple[str, FixtureFn]] = [
    # Synthetic SDK-typed fixtures
    ("plain_text", fixture_plain_text),
    ("parallel_tool_calls", fixture_parallel_tool_calls),
    ("thinking_then_text", fixture_thinking_then_text),
    # Real wire-format dicts lifted from tests/unit/test_llm_streaming.py
    ("real_text_basic", fixture_real_text_basic),
    ("real_thinking_delta_accumulates", fixture_real_thinking_delta_accumulates),
    ("real_thinking_then_text", fixture_real_thinking_then_text),
    ("real_thinking_delta_without_block_start", fixture_real_thinking_delta_without_block_start),
    ("real_eager_streaming_none_initial_fields", fixture_real_eager_streaming_none_initial_fields),
    ("real_concatenated_tool_json", fixture_real_concatenated_tool_json),
    # Live recordings from northgate.xiaobei.top gateway (deepseek-v4-pro-anthropic)
    ("rec_plain_text", _make_recording_fixture("plain_text")),
    ("rec_thinking_then_text", _make_recording_fixture("thinking_then_text")),
    ("rec_single_tool_call", _make_recording_fixture("single_tool_call")),
    # Live recordings from real Claude (claude-haiku-4-5, claude-sonnet-4-5,
    # claude-sonnet-4-6) via the same gateway. Critical because real Claude
    # produces authentic ``signature`` values on thinking blocks (deepseek
    # emulator omits these), and exercises real-shape parallel tool_use.
    ("rec_claude_haiku_plain", _make_recording_fixture("claude_haiku_plain")),
    ("rec_claude_thinking_real", _make_recording_fixture("claude_thinking_real")),
    ("rec_claude_thinking_then_tool", _make_recording_fixture("claude_thinking_then_tool")),
    ("rec_claude_single_tool", _make_recording_fixture("claude_single_tool")),
    ("rec_claude_parallel_tools", _make_recording_fixture("claude_parallel_tools")),
    # Round 3: vision + multi-turn + edge cases + model variants
    ("rec_claude_vision", _make_recording_fixture("claude_vision")),
    ("rec_claude_tool_result_followup", _make_recording_fixture("claude_tool_result_followup")),
    ("rec_claude_complex_tool_args", _make_recording_fixture("claude_complex_tool_args")),
    ("rec_claude_with_system", _make_recording_fixture("claude_with_system")),
    ("rec_claude_stop_sequence", _make_recording_fixture("claude_stop_sequence")),
    ("rec_claude_forced_tool", _make_recording_fixture("claude_forced_tool")),
    ("rec_claude_opus46_plain", _make_recording_fixture("claude_opus46_plain")),
    ("rec_claude_haiku_tool", _make_recording_fixture("claude_haiku_tool")),
    ("rec_claude_very_short", _make_recording_fixture("claude_very_short")),
    ("rec_claude_prefill", _make_recording_fixture("claude_prefill")),
    ("rec_claude_cache_control", _make_recording_fixture("claude_cache_control")),
    ("rec_claude_truncated", _make_recording_fixture("claude_truncated")),
    ("rec_claude_long_text", _make_recording_fixture("claude_long_text")),
    # Round 4: server_tool_use (web_search) + edge cases
    ("rec_server_tool_use", _make_recording_fixture("server_tool_use")),
    ("rec_single_char", _make_recording_fixture("single_char")),
    # Round 5: vendor-truth pair sources — these also drive axis 1 / 2
    # (axis 3 picked up automatically via the .non_stream.json sibling).
    ("rec_vt_plain", _make_recording_fixture("vt_plain")),
    ("rec_vt_tool", _make_recording_fixture("vt_tool")),
    ("rec_vt_thinking", _make_recording_fixture("vt_thinking")),
    ("rec_vt_server_tool_use", _make_recording_fixture("vt_server_tool_use")),
]
