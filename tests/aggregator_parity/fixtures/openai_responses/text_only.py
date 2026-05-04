# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Simple text-only OpenAI Responses fixture using strict SDK types."""

from __future__ import annotations

from openai.types.responses import ResponseStreamEvent
from openai.types.responses.response_content_part_added_event import (
    ResponseContentPartAddedEvent,
)
from openai.types.responses.response_output_item_added_event import (
    ResponseOutputItemAddedEvent,
)
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText
from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent


def fixture_text_only() -> list[ResponseStreamEvent]:
    """Reconstruct expected Message:

    Message(role=ASSISTANT, content=[TextBlock(text="Hello world")])
    """
    return [
        ResponseOutputItemAddedEvent(
            type="response.output_item.added",
            item=ResponseOutputMessage(
                id="msg_text_only_01",
                type="message",
                role="assistant",
                status="in_progress",
                content=[],
            ),
            output_index=0,
            sequence_number=0,
        ),
        ResponseContentPartAddedEvent(
            type="response.content_part.added",
            part=ResponseOutputText(type="output_text", text="", annotations=[]),
            content_index=0,
            item_id="msg_text_only_01",
            output_index=0,
            sequence_number=1,
        ),
        ResponseTextDeltaEvent(
            type="response.output_text.delta",
            delta="Hello",
            content_index=0,
            item_id="msg_text_only_01",
            output_index=0,
            logprobs=[],
            sequence_number=2,
        ),
        ResponseTextDeltaEvent(
            type="response.output_text.delta",
            delta=" world",
            content_index=0,
            item_id="msg_text_only_01",
            output_index=0,
            logprobs=[],
            sequence_number=3,
        ),
    ]
