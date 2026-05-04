# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""OpenAI Responses fixtures lifted from tests/unit/test_llm_streaming.py.

Loose dicts that exercise a comprehensive scenario combining text + function
call + reasoning in a single response. These are normalized to strict SDK
types via ``openai_responses_glue.dict_to_response_event`` before reaching
Set A.
"""

from __future__ import annotations

from typing import Any


def fixture_real_text_tool_reasoning_combined() -> list[dict[str, Any]]:
    """Comprehensive Responses stream with text + function_call + reasoning.

    Inspired by ``test_openai_responses_stream_aggregator_reconstructs_items``
    in test_llm_streaming.py, but rewritten to follow the canonical OpenAI
    wire format that BOTH Set A and Set B expect:

    - Adds ``response.reasoning_summary_part.added`` before
      ``response.reasoning_summary_text.delta`` (Set A requires it to emit
      ThinkingTextMessageStartEvent; Set B doesn't).
    - Initial reasoning ``output_item.added.item.summary`` is empty list
      (real OpenAI streams populate it via summary_part.added events, not
      via the initial item).

    Documenting the divergence: the original test_llm_streaming.py shortcut
    (pre-populated initial summary, skipped summary_part.added) is a real
    Set-A vs Set-B divergence — Set A drops the reasoning summary entirely
    in that case, Set B builds a ReasoningBlock. That divergence is a known
    follow-up for RFC-0023 §阶段 ②/③ to resolve.
    """
    return [
        # Message + text
        {
            "type": "response.output_item.added",
            "item": {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "phase": "commentary",
                "content": [],
            },
        },
        {
            "type": "response.content_part.added",
            "item_id": "msg_1",
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "content_index": 0,
            "delta": "Answer: 42",
        },
        # Function call
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "tool_1",
                "call_id": "tc_1",
                "name": "compute",
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "tool_1",
            "delta": '{"value":',
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "tool_1",
            "delta": " 42}",
        },
        # Closes the function_call: triggers Set A's ToolCallEndEvent
        {
            "type": "response.function_call_arguments.done",
            "item_id": "tool_1",
            "name": "compute",
            "arguments": '{"value": 42}',
        },
        # Reasoning (canonical: empty initial summary, summary_part.added then deltas)
        {
            "type": "response.output_item.added",
            "item": {
                "type": "reasoning",
                "id": "rs_reason_1",
                "summary": [],
            },
        },
        {
            "type": "response.reasoning_summary_part.added",
            "item_id": "rs_reason_1",
            "summary_index": 0,
            "part": {"type": "summary_text", "text": ""},
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs_reason_1",
            "summary_index": 0,
            "delta": "Checked prior calculations",
        },
        # Closes the reasoning summary: triggers Set A's ThinkingTextMessageEndEvent
        {
            "type": "response.reasoning_summary_text.done",
            "item_id": "rs_reason_1",
            "summary_index": 0,
            "text": "Checked prior calculations",
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "model": "gpt-4.1",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]
