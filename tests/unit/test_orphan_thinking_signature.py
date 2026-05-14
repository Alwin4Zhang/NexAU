"""Orphan thinking_delta → empty signature regression.

When Anthropic (esp. Bedrock claude-opus-4.x) emits a thinking block
that gets a `thinking_delta` but no matching `signature_delta` before
`content_block_stop`, the SDK ThinkingBlock's required `signature: str`
field defaults to "" — and that empty string leaks through to UMP
ReasoningBlock.signature, then into persistence, then into the next
turn's outbound payload where `serialize_ump_to_anthropic_messages_payload`
runs `if block.signature:` (falsy), falls past the thinking branch, and
demotes the reasoning to a plain `text` block. User-visible: agent's
internal thinking leaks into the conversation as if it were the actual
reply.

Real-world repro found in a customer-supplied nexau.db: 1462 of 2755
agent_run_actions rows contained reasoning blocks with `"signature":""`.

Fix: `ModelResponse.from_anthropic_message` coerces empty signature to
None at the UMP boundary, so persisted ReasoningBlocks carry the
canonical "unsigned" signal.
"""

from __future__ import annotations

from anthropic.types import (
    Message as AnthropicMessage,
)
from anthropic.types import (
    TextBlock as AnthropicTextBlock,
)
from anthropic.types import (
    ThinkingBlock as AnthropicThinkingBlock,
)
from anthropic.types import (
    Usage,
)

from nexau.archs.main_sub.execution.model_response import ModelResponse


def _make_anthropic_message(thinking_text: str, signature: str, reply_text: str) -> AnthropicMessage:
    """Build a non-stream Anthropic message with a thinking block + text block."""
    return AnthropicMessage(
        id="msg_test",
        type="message",
        role="assistant",
        model="claude-opus-4-6",
        stop_reason="end_turn",
        stop_sequence=None,
        usage=Usage(input_tokens=10, output_tokens=20),
        content=[
            AnthropicThinkingBlock(type="thinking", thinking=thinking_text, signature=signature),
            AnthropicTextBlock(type="text", text=reply_text),
        ],
    )


def test_normal_thinking_signature_preserved() -> None:
    """Sanity: when signature is non-empty, it round-trips intact."""
    msg = _make_anthropic_message("reasoning step 1", "real-sig-xyz", "the answer is 42")
    mr = ModelResponse.from_anthropic_message(msg)
    assert mr.reasoning_signature == "real-sig-xyz"
    assert mr.reasoning_content == "reasoning step 1"
    assert mr.content == "the answer is 42"


def test_empty_signature_coerced_to_none() -> None:
    """Bug regression: orphan thinking_delta (signature='') must NOT leak
    into UMP. ModelResponse.reasoning_signature should be None, not ''.

    Without this fix:
      - reasoning_signature stays '' through to persistence
      - next turn's outbound payload checks `if block.signature:` (falsy)
      - thinking block gets demoted to text → reasoning leaks to user
    """
    msg = _make_anthropic_message("reasoning step 1", "", "the answer is 42")
    mr = ModelResponse.from_anthropic_message(msg)
    assert mr.reasoning_signature is None, f"empty signature should coerce to None, got {mr.reasoning_signature!r}"
    # Reasoning content still preserved — the fix only normalises the
    # signature field, not the thinking text.
    assert mr.reasoning_content == "reasoning step 1"


def test_to_ump_message_no_signature_field_when_unsigned() -> None:
    """UMP ReasoningBlock built from to_ump_message() has signature=None
    when upstream was orphan-thinking. The downstream serializer's
    `if block.signature:` check then routes correctly (caller's policy:
    drop, fallback to text, or send unsigned thinking)."""
    from nexau.core.messages import ReasoningBlock

    msg = _make_anthropic_message("reasoning", "", "reply")
    mr = ModelResponse.from_anthropic_message(msg)
    ump = mr.to_ump_message()
    reasoning_blocks = [b for b in ump.content if isinstance(b, ReasoningBlock)]
    assert len(reasoning_blocks) == 1
    assert reasoning_blocks[0].signature is None


# ============================================================================
# Layer 3 — Serializer drops orphan thinking instead of demoting to text
# ============================================================================
# (Legacy `signature=""` rows are handled here too: the serializer's
# `if block.signature:` truthy check treats "" and None identically →
# both fall into the unsigned default-DROP branch. No separate load-side
# validator needed.)


def test_serializer_drops_unsigned_thinking_by_default() -> None:
    """Outbound payload: ReasoningBlock with no signature (and no
    `allow_unsigned_thinking` opt-in) is DROPPED, not demoted to a text
    block. Reasoning content stays in persistence but doesn't leak into
    the LLM context.

    Pre-fix: serializer's `elif block.text:` fallback appended
    `{"type": "text", "text": reasoning_text}` to the outbound content,
    causing the agent's internal reasoning to appear as if it were the
    assistant's actual reply.
    """
    from nexau.core.messages import Message, ReasoningBlock, Role, TextBlock
    from nexau.core.serializers.anthropic_messages import (
        serialize_ump_to_anthropic_messages_payload,
    )

    msgs = [
        Message(
            role=Role.ASSISTANT,
            content=[
                ReasoningBlock(text="internal reasoning, should NOT leak", signature=None),
                TextBlock(text="the actual reply"),
            ],
        ),
    ]
    _system, convo = serialize_ump_to_anthropic_messages_payload(msgs)
    assert len(convo) == 1
    blocks = convo[0]["content"]
    # Only one block: the text reply. Reasoning was dropped.
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"] == "the actual reply"
    # Critically: the reasoning text does NOT appear anywhere in outbound.
    assert "internal reasoning, should NOT leak" not in str(convo)


def test_serializer_keeps_signed_thinking_and_drops_only_unsigned() -> None:
    """Mixed case: signed reasoning passes through as a thinking block;
    unsigned reasoning in the same message is dropped."""
    from nexau.core.messages import Message, ReasoningBlock, Role, TextBlock
    from nexau.core.serializers.anthropic_messages import (
        serialize_ump_to_anthropic_messages_payload,
    )

    msgs = [
        Message(
            role=Role.ASSISTANT,
            content=[
                ReasoningBlock(text="signed reasoning", signature="real-sig"),
                ReasoningBlock(text="orphan reasoning", signature=None),
                TextBlock(text="reply"),
            ],
        ),
    ]
    _system, convo = serialize_ump_to_anthropic_messages_payload(msgs)
    blocks = convo[0]["content"]
    # Signed thinking + text reply = 2 blocks (orphan dropped)
    assert len(blocks) == 2
    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["signature"] == "real-sig"
    assert blocks[1]["type"] == "text"


def test_strip_thinking_signatures_clears_all_and_layer3_drops_them() -> None:
    """Layer 4 (recovery): `_strip_thinking_signatures` clears every
    ReasoningBlock signature to None, then the existing serializer
    (Layer 3 default branch) drops them all naturally — no new flag
    needed through the adapter/serializer chain.

    Validates the "copy the existing pattern" decision: we re-use
    Layer 3's unsigned-thinking-drop path instead of inventing a parallel
    `drop_all_thinking` parameter.
    """
    from nexau.archs.main_sub.execution.llm_caller import _strip_thinking_signatures
    from nexau.core.messages import Message, ReasoningBlock, Role, TextBlock
    from nexau.core.serializers.anthropic_messages import (
        serialize_ump_to_anthropic_messages_payload,
    )

    original = [
        Message(
            role=Role.ASSISTANT,
            content=[
                ReasoningBlock(text="signed reasoning", signature="real-sig"),
                ReasoningBlock(text="unsigned reasoning", signature=None),
                TextBlock(text="reply"),
            ],
        ),
    ]

    sanitised = _strip_thinking_signatures(original)

    # 1. Original is untouched (helper returns a copy)
    signed_block = original[0].content[0]
    assert isinstance(signed_block, ReasoningBlock)
    assert signed_block.signature == "real-sig"

    # 2. Sanitised history has all signatures cleared
    for msg in sanitised:
        for block in msg.content:
            if isinstance(block, ReasoningBlock):
                assert block.signature is None

    # 3. Feeding the sanitised history through the existing serializer
    #    drops every reasoning block via the Layer 3 default branch —
    #    no `drop_all_thinking` parameter required.
    _system, convo = serialize_ump_to_anthropic_messages_payload(sanitised)
    blocks = convo[0]["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"] == "reply"
    assert "signed reasoning" not in str(convo)
    assert "unsigned reasoning" not in str(convo)


def test_serializer_only_reasoning_message_emits_stub_not_leak() -> None:
    """Pathological case: an assistant message contains ONLY a
    ReasoningBlock and no companion content (truncated stream /
    aggregator bug / hand-built UMP). Dropping it would leave
    content=[] which Anthropic rejects, AND would break user/assistant
    alternation if the message sits in the middle of history.

    Fix: emit a stub `[reasoning omitted]` text block. Same guarantee
    as the companion-present DROP path — the actual reasoning text
    NEVER appears in outbound. The model sees an empty placeholder
    turn, not the agent's internal stream-of-consciousness.
    """
    from nexau.core.messages import Message, ReasoningBlock, Role, TextBlock
    from nexau.core.serializers.anthropic_messages import (
        serialize_ump_to_anthropic_messages_payload,
    )

    secret_reasoning = "INTERNAL_STREAM_OF_CONSCIOUSNESS_MUST_NEVER_LEAK"
    msgs = [
        Message(role=Role.USER, content=[TextBlock(text="hi")]),
        Message(
            role=Role.ASSISTANT,
            content=[ReasoningBlock(text=secret_reasoning, signature=None)],
        ),
        Message(role=Role.USER, content=[TextBlock(text="continue")]),
    ]
    _system, convo = serialize_ump_to_anthropic_messages_payload(msgs)

    # 1. The assistant message wasn't dropped (alternation preserved)
    assert len(convo) == 3
    assert convo[1]["role"] == "assistant"

    # 2. Its content is the stub, NOT the secret reasoning
    assistant_blocks = convo[1]["content"]
    assert len(assistant_blocks) == 1
    assert assistant_blocks[0]["type"] == "text"
    assert assistant_blocks[0]["text"] == "[reasoning omitted]"

    # 3. The actual reasoning text appears NOWHERE in outbound
    assert secret_reasoning not in str(convo)


def test_serializer_allow_unsigned_thinking_opt_in_still_works() -> None:
    """When `allow_unsigned_thinking=True`, unsigned reasoning IS sent
    (without signature). The drop only fires under the default policy."""
    from nexau.core.messages import Message, ReasoningBlock, Role
    from nexau.core.serializers.anthropic_messages import (
        serialize_ump_to_anthropic_messages_payload,
    )

    msgs = [
        Message(
            role=Role.ASSISTANT,
            content=[ReasoningBlock(text="unsigned reasoning", signature=None)],
        ),
    ]
    _system, convo = serialize_ump_to_anthropic_messages_payload(msgs, allow_unsigned_thinking=True)
    blocks = convo[0]["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["thinking"] == "unsigned reasoning"
    # No signature field on unsigned-allowed payload.
    assert "signature" not in blocks[0]


# ============================================================================
# Layer 4 — `_is_thinking_signature_error` helper
# ============================================================================


def test_is_thinking_signature_error_matches_real_anthropic_message() -> None:
    """The error string Anthropic actually returns:
    Error code: 400 - Invalid `signature` in `thinking` block
    """
    import anthropic
    import httpx

    from nexau.archs.main_sub.execution.llm_caller import _is_thinking_signature_error

    # Build a real BadRequestError the way the SDK constructs them
    request = httpx.Request("POST", "https://example.com/v1/messages")
    response = httpx.Response(
        400,
        request=request,
        json={
            "error": {
                "type": "internal_error",
                "message": "***.***.content.29: Invalid `signature` in `thinking` block (request id: 20260513...)",
            },
            "type": "error",
        },
    )
    exc = anthropic.BadRequestError(
        message="Invalid `signature` in `thinking` block",
        response=response,
        body=response.json(),
    )
    assert _is_thinking_signature_error(exc) is True


def test_is_thinking_signature_error_rejects_unrelated_400() -> None:
    """A different 400 (e.g. max_tokens exceeded) must NOT match — we
    only want to retry on this specific failure mode."""
    import anthropic
    import httpx

    from nexau.archs.main_sub.execution.llm_caller import _is_thinking_signature_error

    request = httpx.Request("POST", "https://example.com/v1/messages")
    response = httpx.Response(400, request=request, json={"error": {"message": "max_tokens too small"}})
    exc = anthropic.BadRequestError(
        message="max_tokens too small",
        response=response,
        body=response.json(),
    )
    assert _is_thinking_signature_error(exc) is False


def test_is_thinking_signature_error_rejects_non_anthropic_exception() -> None:
    """Plain ValueError, openai errors, etc. — never match."""
    from nexau.archs.main_sub.execution.llm_caller import _is_thinking_signature_error

    assert _is_thinking_signature_error(ValueError("Invalid signature in thinking block")) is False
    assert _is_thinking_signature_error(RuntimeError("anything")) is False


# ============================================================================
# Layer 4 — `_strip_or_raise_on_signature_error` helper
# ============================================================================


def test_strip_or_raise_strips_and_returns_new_params_on_signature_error() -> None:
    """On the signature-error 400, returns a ModelCallParams clone with
    every ReasoningBlock signature cleared."""
    import anthropic
    import httpx
    import pytest as _pytest

    from nexau.archs.main_sub.execution.hooks import ModelCallParams
    from nexau.archs.main_sub.execution.llm_caller import _strip_or_raise_on_signature_error
    from nexau.core.messages import Message, ReasoningBlock, Role, TextBlock

    request = httpx.Request("POST", "https://example.com/v1/messages")
    response = httpx.Response(
        400,
        request=request,
        json={"error": {"type": "internal_error", "message": "Invalid `signature` in `thinking` block"}},
    )
    exc = anthropic.BadRequestError(
        message="Invalid `signature` in `thinking` block",
        response=response,
        body=response.json(),
    )

    params = ModelCallParams(
        messages=[
            Message(role=Role.USER, content=[TextBlock(text="hi")]),
            Message(
                role=Role.ASSISTANT,
                content=[ReasoningBlock(text="r", signature="bogus-sig"), TextBlock(text="answer")],
            ),
        ],
        max_tokens=None,
        force_stop_reason=None,
        agent_state=None,
        tool_call_mode="structured",
        tools=None,
        api_params={},
    )

    new_params = _strip_or_raise_on_signature_error(exc, params, "test")

    # Helper returns a *new* params (dataclasses.replace), not mutates
    assert new_params is not params

    # Every ReasoningBlock signature stripped
    assistant_blocks = new_params.messages[1].content
    reasoning = [b for b in assistant_blocks if isinstance(b, ReasoningBlock)]
    assert len(reasoning) == 1
    assert reasoning[0].signature is None

    # Original untouched
    original_reasoning = [b for b in params.messages[1].content if isinstance(b, ReasoningBlock)]
    assert original_reasoning[0].signature == "bogus-sig"

    # Sanity: with a non-signature 400, the helper re-raises
    other_400 = anthropic.BadRequestError(
        message="max_tokens too small",
        response=httpx.Response(400, request=request, json={"error": {"message": "max_tokens too small"}}),
        body={"error": {"message": "max_tokens too small"}},
    )
    with _pytest.raises(anthropic.BadRequestError, match="max_tokens"):
        _strip_or_raise_on_signature_error(other_400, params, "test")


# ============================================================================
# Tier-1+2 observability (PR #554)
# ============================================================================


def test_layer1_empty_signature_emits_observability_log(caplog) -> None:
    """When ModelResponse.from_anthropic_message coerces signature='' → None,
    a structured log line `thinking_signature.layer1_empty_signature` fires
    with model + thinking_len fields. Production tails this to count how
    often the orphan condition arrives at the UMP write boundary."""
    import logging as _logging

    msg = _make_anthropic_message("reasoning", "", "reply")
    with caplog.at_level(_logging.WARNING):
        ModelResponse.from_anthropic_message(msg)

    matches = [r for r in caplog.records if "layer1_empty_signature" in r.message]
    assert len(matches) == 1, f"expected one layer1 log, got {[r.message for r in caplog.records]}"
    record = matches[0]
    # Structured fields readable by log aggregators
    assert getattr(record, "model", None) == "claude-opus-4-6"
    assert getattr(record, "thinking_len", None) == len("reasoning")
    assert getattr(record, "orphan_thinking_event", None) is True


def test_layer3_drop_emits_observability_log(caplog) -> None:
    """When the serializer drops (or stubs) unsigned reasoning, log fires
    with `branch=drop` or `branch=stub` plus a count of buffered blocks."""
    import logging as _logging

    from nexau.core.messages import Message, ReasoningBlock, Role, TextBlock
    from nexau.core.serializers.anthropic_messages import (
        serialize_ump_to_anthropic_messages_payload,
    )

    # DROP path: unsigned reasoning + companion text → drop
    msgs_drop = [
        Message(
            role=Role.ASSISTANT,
            content=[ReasoningBlock(text="r1", signature=None), TextBlock(text="answer")],
        ),
    ]
    with caplog.at_level(_logging.WARNING):
        serialize_ump_to_anthropic_messages_payload(msgs_drop)
    drop_logs = [r for r in caplog.records if "layer3_drop_unsigned" in r.message]
    assert len(drop_logs) == 1
    assert getattr(drop_logs[0], "branch", None) == "drop"
    assert getattr(drop_logs[0], "count", None) == 1

    caplog.clear()

    # STUB path: unsigned reasoning alone → stub
    msgs_stub = [
        Message(
            role=Role.ASSISTANT,
            content=[ReasoningBlock(text="r1", signature=None)],
        ),
    ]
    with caplog.at_level(_logging.WARNING):
        serialize_ump_to_anthropic_messages_payload(msgs_stub)
    stub_logs = [r for r in caplog.records if "layer3_drop_unsigned" in r.message]
    assert len(stub_logs) == 1
    assert getattr(stub_logs[0], "branch", None) == "stub"
