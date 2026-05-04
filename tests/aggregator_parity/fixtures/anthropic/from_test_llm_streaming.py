# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Real-world Anthropic event sequences lifted from tests/unit/test_llm_streaming.py.

These are loose-dict fixtures that exercise wire-format edge cases observed in
production (eager_input_streaming None initial fields, thinking_delta before
content_block_start, concatenated tool input JSON from eager streaming, etc.).
They were originally Set B regression tests; running them through the parity
harness asserts that Set A produces an equivalent Message on the same input.

Loose dicts are normalized to strict SDK types via
``anthropic_glue.dict_to_anthropic_event`` before reaching Set A. Set B
consumes them as-is.

Known divergence (NOT lifted):

- ``test_anthropic_stream_aggregator_does_not_overwrite_tool_block_on_duplicate_starts``
  — uses ``id: None, name: None`` on a duplicate ``content_block_start``. Set B
  permissively merges (preserving prior id/name); Anthropic SDK strictly rejects
  ``None`` on these fields. This is a real Set-B-only edge case (wire-level
  pathology that doesn't reach the SDK parser in production); parity testing
  isn't meaningful here.
"""

from __future__ import annotations

from typing import Any


def fixture_real_text_basic() -> list[dict[str, Any]]:
    """Simple text response — two text deltas concatenated.

    Lifted from ``test_anthropic_stream_aggregator_builds_message_blocks``.
    """
    return [
        {"type": "message_start", "message": {"role": "assistant", "model": "claude-3"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " there"}},
        {"type": "content_block_stop", "index": 0},
    ]


def fixture_real_thinking_delta_accumulates() -> list[dict[str, Any]]:
    """Multiple thinking_delta events accumulate into one ReasoningBlock.

    Lifted from ``test_anthropic_stream_aggregator_thinking_delta_accumulates``.
    """
    return [
        {"type": "message_start", "message": {"role": "assistant", "model": "claude-3"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Step 1: "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "analyze the problem"}},
        {"type": "content_block_stop", "index": 0},
    ]


def fixture_real_thinking_then_text() -> list[dict[str, Any]]:
    """Thinking block followed by text block — both should appear in order.

    Lifted from ``test_anthropic_stream_aggregator_thinking_then_text``. Note
    this overlaps with the synthetic ``thinking_then_text`` fixture but uses
    the loose dict form that exercises the dict-normalization path.
    """
    return [
        {"type": "message_start", "message": {"role": "assistant", "model": "claude-3"}},
        # Thinking
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Let me think"}},
        {"type": "content_block_stop", "index": 0},
        # Text
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "The answer is 42"}},
        {"type": "content_block_stop", "index": 1},
    ]


def fixture_real_thinking_delta_without_block_start() -> list[dict[str, Any]]:
    """Edge case: thinking_delta arrives before any content_block_start.

    Lifted from ``test_anthropic_stream_aggregator_thinking_delta_without_block_start``.
    Set B handles this by inferring the block type from the delta. Set A may
    or may not — this fixture surfaces the answer.
    """
    return [
        {"type": "message_start", "message": {"role": "assistant"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}},
        {"type": "content_block_stop", "index": 0},
    ]


def fixture_real_eager_streaming_none_initial_fields() -> list[dict[str, Any]]:
    """Regression: eager_input_streaming sets text/thinking to None on content_block_start.

    Lifted from ``test_anthropic_stream_aggregator_handles_none_initial_fields``.
    Set B's ``block.get("text", "") or ""`` handles None. Set A receives this
    via the SDK normalization (None → empty str).
    """
    return [
        {"type": "message_start", "message": {"role": "assistant", "model": "claude-4"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": None}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "thinking", "thinking": None}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "thinking_delta", "thinking": "Let me think"}},
        {"type": "content_block_stop", "index": 1},
    ]


def fixture_real_concatenated_tool_json() -> list[dict[str, Any]]:
    """Regression: eager_input_streaming may produce concatenated JSON in tool input.

    Lifted from ``test_anthropic_stream_aggregator_concatenated_tool_json``.
    Set B's ``raw_decode`` fallback extracts the first valid JSON object;
    the parity reconstructor mirrors this fallback so the parsed input matches.
    """
    return [
        {"type": "message_start", "message": {"role": "assistant", "model": "claude-4"}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"pattern":"a"}{"pattern":"b"}'},
        },
        {"type": "content_block_stop", "index": 0},
    ]
