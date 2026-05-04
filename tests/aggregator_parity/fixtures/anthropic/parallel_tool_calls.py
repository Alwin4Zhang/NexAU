# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Two parallel tool calls after a brief assistant text.

Tests that the harness preserves block ordering across text → tool_use → tool_use.
"""

from __future__ import annotations

from anthropic.types import (
    InputJSONDelta,
    Message,
    MessageDeltaUsage,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStopEvent,
    RawMessageStreamEvent,
    TextBlock,
    TextDelta,
    ToolUseBlock,
    Usage,
)
from anthropic.types.raw_message_delta_event import Delta


def fixture_parallel_tool_calls() -> list[RawMessageStreamEvent]:
    """Reconstruct expected Message:

    Message(role=ASSISTANT, content=[
        TextBlock(text="Let me check both."),
        ToolUseBlock(id="toolu_01", name="get_weather",
                     input={"location": "Beijing"}),
        ToolUseBlock(id="toolu_02", name="get_time", input={"tz": "UTC"}),
    ])
    """
    return [
        RawMessageStartEvent(
            type="message_start",
            message=Message(
                id="msg_parallel_tools_01",
                type="message",
                role="assistant",
                content=[],
                model="claude-sonnet-4-20250514",
                stop_reason=None,
                stop_sequence=None,
                usage=Usage(input_tokens=20, output_tokens=0),
            ),
        ),
        # Text block at index 0
        RawContentBlockStartEvent(
            type="content_block_start",
            index=0,
            content_block=TextBlock(type="text", text=""),
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta",
            index=0,
            delta=TextDelta(type="text_delta", text="Let me check both."),
        ),
        RawContentBlockStopEvent(type="content_block_stop", index=0),
        # First tool at index 1
        RawContentBlockStartEvent(
            type="content_block_start",
            index=1,
            content_block=ToolUseBlock(
                type="tool_use",
                id="toolu_01",
                name="get_weather",
                input={},
            ),
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta",
            index=1,
            delta=InputJSONDelta(type="input_json_delta", partial_json='{"location"'),
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta",
            index=1,
            delta=InputJSONDelta(type="input_json_delta", partial_json=': "Beijing"}'),
        ),
        RawContentBlockStopEvent(type="content_block_stop", index=1),
        # Second tool at index 2
        RawContentBlockStartEvent(
            type="content_block_start",
            index=2,
            content_block=ToolUseBlock(
                type="tool_use",
                id="toolu_02",
                name="get_time",
                input={},
            ),
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta",
            index=2,
            delta=InputJSONDelta(type="input_json_delta", partial_json='{"tz": "UTC"}'),
        ),
        RawContentBlockStopEvent(type="content_block_stop", index=2),
        RawMessageDeltaEvent(
            type="message_delta",
            delta=Delta(stop_reason="tool_use", stop_sequence=None),
            usage=MessageDeltaUsage(output_tokens=15),
        ),
        RawMessageStopEvent(type="message_stop"),
    ]
