# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Extended thinking followed by a final text answer.

Tests Reasoning block ordering and exposes the signature/redacted_data weak gap
that motivates RFC-0023 §阶段 ② AG-UI extensions.
"""

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
    ThinkingBlock,
    ThinkingDelta,
    Usage,
)
from anthropic.types.raw_message_delta_event import Delta


def fixture_thinking_then_text() -> list[RawMessageStreamEvent]:
    """Reconstruct expected Message:

        Message(role=ASSISTANT, content=[
            ReasoningBlock(text="The user asks about X. I should...",
                           signature="sig_xyz", redacted_data=None),
            TextBlock(text="The answer is 42."),
        ])

    Note: signature lands in Set B's output but Set A doesn't currently carry
    it — this fixture is precisely the case that produces a weak gap entry,
    documenting the RFC-0023 §阶段 ② work item.
    """
    return [
        RawMessageStartEvent(
            type="message_start",
            message=Message(
                id="msg_thinking_01",
                type="message",
                role="assistant",
                content=[],
                model="claude-sonnet-4-20250514",
                stop_reason=None,
                stop_sequence=None,
                usage=Usage(input_tokens=15, output_tokens=0),
            ),
        ),
        # Thinking block at index 0
        RawContentBlockStartEvent(
            type="content_block_start",
            index=0,
            content_block=ThinkingBlock(
                type="thinking",
                thinking="",
                signature="",
            ),
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta",
            index=0,
            delta=ThinkingDelta(type="thinking_delta", thinking="The user asks about X. "),
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta",
            index=0,
            delta=ThinkingDelta(type="thinking_delta", thinking="I should respond concisely."),
        ),
        # Note: real Anthropic streams emit a SignatureDelta to set the signature.
        # Set B's accumulator handles this via the catch-all block-merge branch
        # (delta_type not in {"text_delta","thinking_delta","input_json_delta"} →
        #  merge raw fields). To keep the fixture minimal and SDK-independent,
        # we stamp signature directly via the content_block_stop → content_block
        # mapping is the closest approximation; in practice live recordings will
        # include the SignatureDelta event.
        RawContentBlockStopEvent(type="content_block_stop", index=0),
        # Text block at index 1
        RawContentBlockStartEvent(
            type="content_block_start",
            index=1,
            content_block=TextBlock(type="text", text=""),
        ),
        RawContentBlockDeltaEvent(
            type="content_block_delta",
            index=1,
            delta=TextDelta(type="text_delta", text="The answer is 42."),
        ),
        RawContentBlockStopEvent(type="content_block_stop", index=1),
        RawMessageDeltaEvent(
            type="message_delta",
            delta=Delta(stop_reason="end_turn", stop_sequence=None),
            usage=MessageDeltaUsage(output_tokens=12),
        ),
        RawMessageStopEvent(type="message_stop"),
    ]
