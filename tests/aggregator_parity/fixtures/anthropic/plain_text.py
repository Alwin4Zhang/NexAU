# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Plain text response — assistant says hello in a few text deltas."""

from __future__ import annotations

from anthropic.types import (
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
    Usage,
)
from anthropic.types.raw_message_delta_event import Delta


def fixture_plain_text() -> list[RawMessageStreamEvent]:
    """Reconstruct expected Message:

    Message(role=ASSISTANT, content=[TextBlock(text="Hello, world!")])
    """
    return [
        RawMessageStartEvent(
            type="message_start",
            message=Message(
                id="msg_plain_text_01",
                type="message",
                role="assistant",
                content=[],
                model="claude-sonnet-4-20250514",
                stop_reason=None,
                stop_sequence=None,
                usage=Usage(input_tokens=10, output_tokens=0),
            ),
        ),
        RawContentBlockStartEvent(
            type="content_block_start",
            index=0,
            content_block=TextBlock(type="text", text=""),
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta",
            index=0,
            delta=TextDelta(type="text_delta", text="Hello, "),
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta",
            index=0,
            delta=TextDelta(type="text_delta", text="world!"),
        ),
        RawContentBlockStopEvent(type="content_block_stop", index=0),
        RawMessageDeltaEvent(
            type="message_delta",
            delta=Delta(stop_reason="end_turn", stop_sequence=None),
            usage=MessageDeltaUsage(output_tokens=4),
        ),
        RawMessageStopEvent(type="message_stop"),
    ]
