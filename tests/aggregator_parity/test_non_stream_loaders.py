# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Unit tests for the 4 NON_STREAM_LOADERS — direct exercise of edge branches.

The vendor-truth axis (test_stream_vs_non_stream.py) only walks paths that
the 13 recorded `<scenario>.non_stream.json` fixtures happen to hit. This
file pins the loaders' behavior on inputs that aren't covered by those
fixtures: empty payloads, missing required fields, malformed shapes,
multiple tool calls, multiple text parts, etc.

Filename note: avoids `openai`/`chat`/`llm` substrings so conftest's
auto-marker doesn't gate this on a live LLM API key — these are pure
in-memory unit tests.
"""

from __future__ import annotations

from nexau.core.messages import (
    Message,
    ReasoningBlock,
    Role,
    TextBlock,
    ToolUseBlock,
)
from tests.aggregator_parity.parity_helpers import (
    NON_STREAM_LOADERS,
    anthropic_non_stream_json_to_message,
    gemini_non_stream_json_to_message,
    openai_chat_non_stream_json_to_message,
    openai_responses_non_stream_json_to_message,
)

# ============================================================================
# Loader registry shape
# ============================================================================


def test_loader_registry_covers_all_4_providers() -> None:
    assert set(NON_STREAM_LOADERS.keys()) == {
        "anthropic",
        "openai_chat",
        "openai_responses",
        "gemini_rest",
    }


# ============================================================================
# Edge branches: empty / missing / malformed payloads
# ============================================================================


def _assert_empty_assistant_message(msg: Message) -> None:
    assert isinstance(msg, Message)
    assert msg.role == Role.ASSISTANT
    assert msg.content == []


def test_anthropic_loader_empty_payload() -> None:
    _assert_empty_assistant_message(anthropic_non_stream_json_to_message({}))


def test_anthropic_loader_missing_content() -> None:
    """Anthropic responses always carry `content`; absence means the loader
    must default to an empty assistant message, not crash."""
    _assert_empty_assistant_message(anthropic_non_stream_json_to_message({"role": "assistant"}))


def test_anthropic_loader_unknown_block_type_silently_skipped() -> None:
    """Unknown block types must not crash. Per the converter's contract,
    they're skipped (failing strong equivalence is the parity test's job;
    the loader stays robust)."""
    msg = anthropic_non_stream_json_to_message(
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "future_block_type_we_dont_know", "data": "ignored"},
            ],
        }
    )
    assert msg.role == Role.ASSISTANT
    assert len(msg.content) == 1
    assert isinstance(msg.content[0], TextBlock)


def test_anthropic_loader_redacted_thinking() -> None:
    msg = anthropic_non_stream_json_to_message(
        {
            "role": "assistant",
            "content": [{"type": "redacted_thinking", "data": "ENC=="}],
        }
    )
    assert len(msg.content) == 1
    assert isinstance(msg.content[0], ReasoningBlock)
    assert msg.content[0].text == ""
    assert msg.content[0].redacted_data == "ENC=="


def test_anthropic_loader_thinking_with_signature() -> None:
    msg = anthropic_non_stream_json_to_message(
        {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "step 1", "signature": "sig=="}],
        }
    )
    assert isinstance(msg.content[0], ReasoningBlock)
    assert msg.content[0].text == "step 1"
    assert msg.content[0].signature == "sig=="


def test_anthropic_loader_server_tool_use_treated_as_tool() -> None:
    """server_tool_use blocks should map to ToolUseBlock just like tool_use.
    Documented in the converter; a regression that drops this mapping would
    silently lose web_search / etc. blocks from the strong-axis comparison."""
    msg = anthropic_non_stream_json_to_message(
        {
            "role": "assistant",
            "content": [{"type": "server_tool_use", "id": "stu_1", "name": "web_search", "input": {"query": "x"}}],
        }
    )
    assert len(msg.content) == 1
    assert isinstance(msg.content[0], ToolUseBlock)
    assert msg.content[0].name == "web_search"


# ----------------------------------------------------------------------------


def test_oac_loader_empty_payload() -> None:
    _assert_empty_assistant_message(openai_chat_non_stream_json_to_message({}))


def test_oac_loader_empty_choices_list() -> None:
    _assert_empty_assistant_message(openai_chat_non_stream_json_to_message({"choices": []}))


def test_oac_loader_choices_without_message() -> None:
    _assert_empty_assistant_message(openai_chat_non_stream_json_to_message({"choices": [{"index": 0, "finish_reason": "stop"}]}))


def test_oac_loader_string_content_unwraps_correctly() -> None:
    msg = openai_chat_non_stream_json_to_message(
        {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}}],
        }
    )
    assert len(msg.content) == 1
    assert isinstance(msg.content[0], TextBlock)
    assert msg.content[0].text == "hello"


def test_oac_loader_reasoning_content_then_text_then_tools() -> None:
    """Documented order in the Set B converter (reasoning → text → tools).
    Pin it so a refactor doesn't silently reorder."""
    msg = openai_chat_non_stream_json_to_message(
        {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "answer",
                        "reasoning_content": "think",
                        "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": '{"x":1}'}},
                        ],
                    },
                }
            ],
        }
    )
    assert [type(b).__name__ for b in msg.content] == ["ReasoningBlock", "TextBlock", "ToolUseBlock"]
    tool = msg.content[2]
    assert isinstance(tool, ToolUseBlock)
    assert tool.input == {"x": 1}


def test_oac_loader_tool_args_invalid_json_falls_back_to_raw() -> None:
    """Mirrors Set B's recovery path for malformed argument fragments —
    important so a vendor sending invalid JSON doesn't crash the loader."""
    msg = openai_chat_non_stream_json_to_message(
        {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{not valid json"}},
                        ],
                    },
                }
            ],
        }
    )
    assert len(msg.content) == 1
    tool = msg.content[0]
    assert isinstance(tool, ToolUseBlock)
    assert tool.input == {"_raw": "{not valid json"}


# ----------------------------------------------------------------------------


def test_oresp_loader_empty_payload() -> None:
    _assert_empty_assistant_message(openai_responses_non_stream_json_to_message({}))


def test_oresp_loader_empty_output_list() -> None:
    _assert_empty_assistant_message(openai_responses_non_stream_json_to_message({"output": []}))


def test_oresp_loader_message_with_multiple_text_parts_concatenates() -> None:
    msg = openai_responses_non_stream_json_to_message(
        {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "hello "},
                        {"type": "output_text", "text": "world"},
                    ],
                }
            ],
        }
    )
    assert len(msg.content) == 1
    block = msg.content[0]
    assert isinstance(block, TextBlock)
    assert block.text == "hello world"


def test_oresp_loader_function_call_with_invalid_json() -> None:
    msg = openai_responses_non_stream_json_to_message(
        {
            "output": [
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_x",
                    "name": "f",
                    "arguments": "not-json",
                }
            ],
        }
    )
    assert len(msg.content) == 1
    tool = msg.content[0]
    assert isinstance(tool, ToolUseBlock)
    assert tool.id == "call_x"  # call_id preferred over id
    assert tool.input == {"_raw": "not-json"}


def test_oresp_loader_reasoning_with_encrypted_content() -> None:
    msg = openai_responses_non_stream_json_to_message(
        {
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "thoughts"}],
                    "encrypted_content": "ENC==",
                }
            ],
        }
    )
    assert len(msg.content) == 1
    block = msg.content[0]
    assert isinstance(block, ReasoningBlock)
    assert block.text == "thoughts"
    assert block.redacted_data == "ENC=="


# ----------------------------------------------------------------------------


def test_gemini_loader_empty_payload() -> None:
    _assert_empty_assistant_message(gemini_non_stream_json_to_message({}))


def test_gemini_loader_no_candidates() -> None:
    _assert_empty_assistant_message(gemini_non_stream_json_to_message({"candidates": []}))


def test_gemini_loader_candidate_without_content() -> None:
    _assert_empty_assistant_message(gemini_non_stream_json_to_message({"candidates": [{"finishReason": "STOP"}]}))


def test_gemini_loader_thought_text_with_separate_signature_part() -> None:
    """Gemini emits thoughtSignature in its own part; the loader must scan
    for it and attach to any ReasoningBlock it produces. Regression here
    would silently lose reasoning signature."""
    msg = gemini_non_stream_json_to_message(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"text": "thinking aloud", "thought": True},
                            {"thoughtSignature": "SIG=="},
                            {"text": "answer"},
                        ],
                    }
                }
            ],
        }
    )
    assert [type(b).__name__ for b in msg.content] == ["ReasoningBlock", "TextBlock"]
    reasoning = msg.content[0]
    assert isinstance(reasoning, ReasoningBlock)
    assert reasoning.signature == "SIG=="


def test_gemini_loader_function_call_id_synthesized_per_index() -> None:
    """Gemini doesn't carry a tool_call_id on the wire. Both Set A's
    aggregator and this loader must agree on the synthesized id format
    (``gemini_tc_{i}``); see GeminiRestEventAggregator naming convention.
    A drift here breaks vendor-truth axis silently for multi-tool fixtures."""
    msg = gemini_non_stream_json_to_message(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": "a", "args": {"x": 1}}},
                            {"functionCall": {"name": "b", "args": {"y": 2}}},
                        ],
                    }
                }
            ],
        }
    )
    assert len(msg.content) == 2
    a, b = msg.content
    assert isinstance(a, ToolUseBlock)
    assert isinstance(b, ToolUseBlock)
    assert a.id == "gemini_tc_0"
    assert b.id == "gemini_tc_1"
