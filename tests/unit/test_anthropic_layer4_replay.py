"""Layer 4 retry — HTTP-replay regression suite.

Replaces the four corresponding `test_layer4_*` cases in
`tests/integration_live/test_anthropic_thinking_signature_live.py`. The
live tests are kept around only as drift detection (the `live_nightly`
marker keeps them out of PR CI); for regression coverage the replay
versions below are strictly better:

  - Deterministic — no upstream flake on Bedrock thinking pathologies.
  - No API key burn, no network — every contributor / fork-PR can run them.
  - Asserts the retry POST body **does not contain any `thinking` block**
    (a guarantee live tests can't make because they can't read the
    request body the SDK sent).

Real path exercised (only the wire layer is mocked):

  UMP messages
    → AnthropicMessagesAdapter (real serializer)
    → anthropic.Anthropic / AsyncAnthropic (real SDK)
    → httpx (intercepted by respx)
    ← 400 invalid-signature body (Layer 4 trigger)
    → _strip_or_raise_on_signature_error (real helper)
    → AnthropicMessagesAdapter (re-serialized without thinking)
    → SDK → httpx (intercepted)
    ← 200 success body / SSE stream
    → ModelResponse.from_anthropic_message (real adapter)
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx
import pytest
import respx

from nexau.archs.main_sub.execution.hooks import ModelCallParams
from nexau.archs.main_sub.execution.llm_caller import (
    call_llm_with_anthropic_chat_completion,
    call_llm_with_anthropic_chat_completion_async,
)
from nexau.core.messages import Message, ReasoningBlock, Role, TextBlock

# ── Fixtures ────────────────────────────────────────────────────────────────

_BASE_URL = "https://mock-anthropic.local"
_API_KEY = "test-key-not-real"
_MODEL = "claude-sonnet-4-5-20250929"
_POISON_SIG = "BOGUS_LEGACY_SIGNATURE_THAT_GATEWAY_WILL_REJECT"

# The exact error shape Anthropic returns for the production bug, captured
# from a real Bedrock claude-opus-4.x rejection (see PR #554 commit f8e6faa5).
_INVALID_SIGNATURE_400 = {
    "type": "error",
    "error": {
        "type": "internal_error",
        "message": "***.***.content.29: Invalid `signature` in `thinking` block",
    },
}

# Minimal valid non-stream success — just enough for ModelResponse to extract.
_SUCCESS_NONSTREAM = {
    "id": "msg_replay_success",
    "type": "message",
    "role": "assistant",
    "model": _MODEL,
    "content": [{"type": "text", "text": "144"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 5},
}


def _sse_event(event_type: str, data: dict[str, Any]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()


def _success_sse_stream() -> bytes:
    """Build a minimal valid Anthropic SSE stream emitting just '144'."""
    return b"".join(
        [
            _sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_replay_stream",
                        "type": "message",
                        "role": "assistant",
                        "model": _MODEL,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 10, "output_tokens": 0},
                    },
                },
            ),
            _sse_event(
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            ),
            _sse_event(
                "content_block_delta",
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "144"}},
            ),
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            ),
            _sse_event("message_stop", {"type": "message_stop"}),
        ]
    )


def _poisoned_history() -> list[Message]:
    """UMP history with a legacy-style ReasoningBlock carrying an invalid
    but non-empty signature. Layers 1-3 keep it (truthy signature passes
    their checks); only Layer 4 can recover."""
    return [
        Message(role=Role.USER, content=[TextBlock(text="What is 8 * 9?")]),
        Message(
            role=Role.ASSISTANT,
            content=[
                ReasoningBlock(text="8 * 9 is 72.", signature=_POISON_SIG),
                TextBlock(text="72"),
            ],
        ),
        Message(role=Role.USER, content=[TextBlock(text="Now multiply that by 2.")]),
    ]


def _params() -> ModelCallParams:
    return ModelCallParams(
        messages=_poisoned_history(),
        max_tokens=None,
        force_stop_reason=None,
        agent_state=None,
        tool_call_mode="structured",
        tools=None,
        api_params={},
    )


def _assistant_blocks(captured_post_body: bytes) -> list[dict[str, Any]]:
    """Pull the assistant message's content blocks out of a captured POST
    body so retry-side assertions can verify the thinking block is gone."""
    payload = json.loads(captured_post_body)
    for msg in payload["messages"]:
        if msg["role"] == "assistant":
            return list(msg["content"])
    return []


# ── Tests ───────────────────────────────────────────────────────────────────


def test_layer4_sync_nonstream_replay() -> None:
    """Sync non-stream: gateway returns 400 → retry strips signatures →
    gateway accepts → ModelResponse carries the success text.

    Also asserts the retry POST body no longer contains any thinking block.
    """
    captured: list[httpx.Request] = []

    def _on_request(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 1:
            return httpx.Response(400, json=_INVALID_SIGNATURE_400)
        return httpx.Response(200, json=_SUCCESS_NONSTREAM)

    with respx.mock(base_url=_BASE_URL) as router:
        router.post("/v1/messages").mock(side_effect=_on_request)
        client = anthropic.Anthropic(api_key=_API_KEY, base_url=_BASE_URL)
        mr = call_llm_with_anthropic_chat_completion(
            client,
            kwargs={"model": _MODEL, "max_tokens": 100},
            model_call_params=_params(),
        )

    assert len(captured) == 2, "should retry exactly once after 400"
    # First POST included the (poisoned) thinking block
    first_blocks = _assistant_blocks(captured[0].content)
    assert any(b["type"] == "thinking" for b in first_blocks)
    # Retry POST stripped every thinking block
    retry_blocks = _assistant_blocks(captured[1].content)
    assert not any(b["type"] == "thinking" for b in retry_blocks), f"retry should drop thinking; got: {[b['type'] for b in retry_blocks]}"
    assert mr.content == "144"


def test_layer4_sync_stream_replay() -> None:
    """Sync streaming: same retry path, with the success delivered as SSE."""
    captured: list[httpx.Request] = []

    def _on_request(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 1:
            return httpx.Response(400, json=_INVALID_SIGNATURE_400)
        return httpx.Response(
            200,
            content=_success_sse_stream(),
            headers={"content-type": "text/event-stream"},
        )

    with respx.mock(base_url=_BASE_URL) as router:
        router.post("/v1/messages").mock(side_effect=_on_request)
        client = anthropic.Anthropic(api_key=_API_KEY, base_url=_BASE_URL)
        mr = call_llm_with_anthropic_chat_completion(
            client,
            kwargs={"model": _MODEL, "max_tokens": 100, "stream": True},
            model_call_params=_params(),
        )

    assert len(captured) == 2
    retry_blocks = _assistant_blocks(captured[1].content)
    assert not any(b["type"] == "thinking" for b in retry_blocks)
    assert mr.content == "144"


def test_layer4_async_nonstream_replay() -> None:
    """Async non-stream: covers call_llm_with_anthropic_chat_completion_async's
    own retry block (separate code path from sync).

    Uses asyncio.run wrapper instead of @pytest.mark.asyncio per repo
    convention (see tests/integration/test_aggregator_live_e2e.py:662) —
    test-saas's xdist setup runs pytest-asyncio in STRICT mode where
    plain ``async def test_*`` raises 'async def functions are not
    natively supported'.
    """
    import asyncio

    captured: list[httpx.Request] = []

    def _on_request(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 1:
            return httpx.Response(400, json=_INVALID_SIGNATURE_400)
        return httpx.Response(200, json=_SUCCESS_NONSTREAM)

    async def _run() -> Any:
        client = anthropic.AsyncAnthropic(api_key=_API_KEY, base_url=_BASE_URL)
        return await call_llm_with_anthropic_chat_completion_async(
            client,
            kwargs={"model": _MODEL, "max_tokens": 100},
            model_call_params=_params(),
        )

    with respx.mock(base_url=_BASE_URL) as router:
        router.post("/v1/messages").mock(side_effect=_on_request)
        mr = asyncio.run(_run())

    assert len(captured) == 2
    retry_blocks = _assistant_blocks(captured[1].content)
    assert not any(b["type"] == "thinking" for b in retry_blocks)
    assert mr.content == "144"


def test_layer4_async_stream_replay() -> None:
    """Async streaming: 4th and last Layer 4 path.

    asyncio.run wrapper per repo convention (see sibling test for details).
    """
    import asyncio

    captured: list[httpx.Request] = []

    def _on_request(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 1:
            return httpx.Response(400, json=_INVALID_SIGNATURE_400)
        return httpx.Response(
            200,
            content=_success_sse_stream(),
            headers={"content-type": "text/event-stream"},
        )

    async def _run() -> Any:
        client = anthropic.AsyncAnthropic(api_key=_API_KEY, base_url=_BASE_URL)
        return await call_llm_with_anthropic_chat_completion_async(
            client,
            kwargs={"model": _MODEL, "max_tokens": 100, "stream": True},
            model_call_params=_params(),
        )

    with respx.mock(base_url=_BASE_URL) as router:
        router.post("/v1/messages").mock(side_effect=_on_request)
        mr = asyncio.run(_run())

    assert len(captured) == 2
    retry_blocks = _assistant_blocks(captured[1].content)
    assert not any(b["type"] == "thinking" for b in retry_blocks)
    assert mr.content == "144"


def test_layer4_non_signature_400_propagates_no_retry() -> None:
    """A 400 for a different reason (e.g. max_tokens) propagates unchanged
    — Layer 4 only catches the specific signature error."""
    captured: list[httpx.Request] = []

    def _on_request(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            400,
            json={"type": "error", "error": {"type": "invalid_request_error", "message": "max_tokens too small"}},
        )

    with respx.mock(base_url=_BASE_URL) as router:
        router.post("/v1/messages").mock(side_effect=_on_request)
        client = anthropic.Anthropic(api_key=_API_KEY, base_url=_BASE_URL)
        with pytest.raises(anthropic.BadRequestError, match="max_tokens"):
            call_llm_with_anthropic_chat_completion(
                client,
                kwargs={"model": _MODEL, "max_tokens": 1},
                model_call_params=_params(),
            )

    assert len(captured) == 1, "non-signature 400 must not retry"
