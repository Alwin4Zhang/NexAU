"""Drift-detection live tests for the orphan-thinking-signature fix.

All 4 tests in this file are tagged ``live_nightly`` and ONLY run in the
nightly workflow. They hit a real Anthropic-compatible gateway and
assert *upstream invariants* that can flake on Bedrock/claude-opus-4.x
even when our code is correct:

  - Every thinking block emits ``signature_delta`` before
    ``content_block_stop`` (no orphan)
  - The gateway accepts our serializer's echo-back of signed thinking
  - An unsigned ReasoningBlock in history doesn't leak as text
  - Multi-iteration agent loops stay signature-stable

Skipped unless these env vars are set (PR CI's ``test-saas`` job and the
nightly workflow both inject them via ``secrets.NORTHGATE_API_KEY``):

  LIVE_ANTHROPIC_BASE_URL=https://your-gateway.example.com
  LIVE_ANTHROPIC_API_KEY=sk-...
  LIVE_ANTHROPIC_MODEL=claude-opus-4-6  (optional; defaults to opus 4.6)

Per-PR regression coverage for Layer 4's retry-on-invalid-signature path
lives in ``tests/unit/test_anthropic_layer4_replay.py`` — HTTP-replay via
respx, deterministic, no key burn, no flake. The replay tests cover all
4 (sync/async × stream/non-stream) paths plus the no-retry-on-other-400
guarantee, and additionally assert the retry POST body doesn't contain
any thinking block (a guarantee live tests can't make).
"""

from __future__ import annotations

import os
from typing import Any

import pytest

anthropic = pytest.importorskip("anthropic")

# E402: these imports must come AFTER importorskip — `anthropic` package
# is required and the live tests skip cleanly when it's missing. Suppress
# the rule rather than pull all imports into each function.
from anthropic.types import (  # noqa: E402
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
)

from nexau.archs.main_sub.execution.model_response import ModelResponse  # noqa: E402
from nexau.core.messages import Message, ReasoningBlock, Role, TextBlock  # noqa: E402
from nexau.core.serializers.anthropic_messages import (  # noqa: E402
    serialize_ump_to_anthropic_messages_payload,
)

BASE_URL = os.environ.get("LIVE_ANTHROPIC_BASE_URL", "")
API_KEY = os.environ.get("LIVE_ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("LIVE_ANTHROPIC_MODEL", "claude-opus-4-6")

pytestmark = pytest.mark.skipif(
    not (BASE_URL and API_KEY),
    reason="Live tests need LIVE_ANTHROPIC_BASE_URL + LIVE_ANTHROPIC_API_KEY",
)


def _client() -> Any:
    # mypy: `anthropic` is loaded via `importorskip` so it has no concrete
    # type at lint time. Returning Any is the simplest stable form.
    return anthropic.Anthropic(api_key=API_KEY, base_url=BASE_URL)


def _drive_and_count(stream) -> tuple[ModelResponse, list[dict]]:
    """Walk an SDK stream, returning (ModelResponse, per-block stats).

    The per-block stats let tests assert "every thinking block had its
    signature_delta" without needing to know the exact block count
    upfront.
    """
    block_stats: list[dict] = []
    for event in stream:
        if isinstance(event, RawContentBlockStartEvent):
            block_stats.append(
                {
                    "idx": event.index,
                    "kind": getattr(event.content_block, "type", None),
                    "thinking_deltas": 0,
                    "signature_deltas": 0,
                }
            )
        elif isinstance(event, RawContentBlockDeltaEvent) and block_stats:
            t = getattr(event.delta, "type", None)
            if t == "thinking_delta":
                block_stats[-1]["thinking_deltas"] += 1
            elif t == "signature_delta":
                block_stats[-1]["signature_deltas"] += 1
    final = stream.get_final_message()
    return ModelResponse.from_anthropic_message(final), block_stats


def _assert_no_orphan(stats: list[dict]) -> None:
    """For every thinking block in the stream, signature_delta arrived."""
    for s in stats:
        if s["kind"] == "thinking":
            assert s["signature_deltas"] >= 1, (
                f"orphan thinking detected: block[{s['idx']}] had "
                f"{s['thinking_deltas']} thinking_delta(s) but "
                f"{s['signature_deltas']} signature_delta(s)"
            )


@pytest.mark.live_nightly
def test_single_turn_thinking_emits_signature_delta() -> None:
    """Wire-level baseline: a single-turn thinking call always gets
    `signature_delta` before `content_block_stop`."""
    client = _client()
    with client.messages.stream(
        model=MODEL,
        max_tokens=1500,
        thinking={"type": "enabled", "budget_tokens": 1024},
        messages=[{"role": "user", "content": "What is 12 * 17? Think briefly."}],
    ) as stream:
        mr, stats = _drive_and_count(stream)
    _assert_no_orphan(stats)
    assert mr.reasoning_signature, "ModelResponse should carry a non-empty signature"
    assert mr.content and "204" in mr.content, f"expected '204' in reply, got: {mr.content!r}"


@pytest.mark.live_nightly
def test_multi_turn_echoback_signed_thinking_accepted() -> None:
    """Multi-turn: turn 1 produces signed thinking; turn 2 echoes it back
    through nexau's serializer and Anthropic accepts the replay."""
    client = _client()
    with client.messages.stream(
        model=MODEL,
        max_tokens=1500,
        thinking={"type": "enabled", "budget_tokens": 1024},
        messages=[{"role": "user", "content": "What is 8 * 9?"}],
    ) as stream:
        mr1, stats1 = _drive_and_count(stream)
    _assert_no_orphan(stats1)
    assert mr1.reasoning_signature, "turn 1 must produce a real signature"

    # Build turn 2 via UMP → nexau's Anthropic serializer
    ump_msgs = [
        Message(role=Role.USER, content=[TextBlock(text="What is 8 * 9?")]),
        mr1.to_ump_message(),
        Message(role=Role.USER, content=[TextBlock(text="Now multiply that by 2.")]),
    ]
    _system, convo = serialize_ump_to_anthropic_messages_payload(ump_msgs)
    assistant_blocks = convo[1]["content"]
    assert any(b["type"] == "thinking" for b in assistant_blocks), "serializer must emit the signed thinking block in outbound"

    with client.messages.stream(
        model=MODEL,
        max_tokens=1500,
        thinking={"type": "enabled", "budget_tokens": 1024},
        messages=convo,
    ) as stream:
        mr2, stats2 = _drive_and_count(stream)
    _assert_no_orphan(stats2)
    assert mr2.content and "144" in mr2.content, f"expected '144' (8*9*2), got: {mr2.content!r}"


@pytest.mark.live_nightly
def test_unsigned_reasoning_dropped_no_leak_in_outbound() -> None:
    """Layer 3 fix: an UNSIGNED ReasoningBlock in history must be DROPPED
    from outbound (not demoted to text). Verifies end-to-end:
      1. Serializer strips the unsigned reasoning
      2. Anthropic accepts the cleaned payload
      3. Model's reply does NOT contain the leaked reasoning text
    """
    leak_canary = "INTERNAL_REASONING_THAT_MUST_NOT_LEAK"
    ump_msgs = [
        Message(role=Role.USER, content=[TextBlock(text="What is 8 * 9?")]),
        Message(
            role=Role.ASSISTANT,
            content=[
                ReasoningBlock(text=leak_canary, signature=None),  # orphan
                TextBlock(text="72"),
            ],
        ),
        Message(role=Role.USER, content=[TextBlock(text="Now multiply that by 3.")]),
    ]
    _system, convo = serialize_ump_to_anthropic_messages_payload(ump_msgs)
    assistant_blocks = convo[1]["content"]
    # Layer 3: unsigned reasoning gone; only the answer text remains
    assert len(assistant_blocks) == 1
    assert assistant_blocks[0]["type"] == "text"
    assert leak_canary not in str(convo)

    client = _client()
    with client.messages.stream(
        model=MODEL,
        max_tokens=1500,
        thinking={"type": "enabled", "budget_tokens": 1024},
        messages=convo,
    ) as stream:
        mr, stats = _drive_and_count(stream)
    _assert_no_orphan(stats)
    assert mr.content and "216" in mr.content, f"expected '216' (72*3), got: {mr.content!r}"
    assert mr.content and leak_canary not in mr.content, f"reasoning leaked into model reply: {mr.content!r}"


@pytest.mark.live_nightly
def test_long_agent_loop_no_orphan_anywhere() -> None:
    """Simulate the user-reported scenario: a long agent loop with
    multiple thinking + tool_use cycles. None of the thinking blocks
    across all turns should be orphan."""
    client = _client()
    tools = [
        {
            "name": "calculator",
            "description": "Evaluate arithmetic.",
            "input_schema": {
                "type": "object",
                "properties": {"expr": {"type": "string"}},
                "required": ["expr"],
            },
        }
    ]
    history: list[dict] = []
    last_user = "What is 5 * 7?"
    for turn in range(3):
        history.append({"role": "user", "content": last_user})
        with client.messages.stream(
            model=MODEL,
            max_tokens=2000,
            thinking={"type": "enabled", "budget_tokens": 1024},
            tools=tools,
            messages=history,
        ) as stream:
            mr, stats = _drive_and_count(stream)
        _assert_no_orphan(stats)
        # Inspect every thinking block in the final message for empty sig
        for i, b in enumerate(mr.raw_message.content):
            if b.type == "thinking":
                assert b.signature != "", f"turn {turn + 1} block[{i}] has empty signature — orphan reproduced!"
        # Build assistant message for next turn
        ac = []
        tu_id = None
        for b in mr.raw_message.content:
            if b.type == "thinking":
                ac.append({"type": "thinking", "thinking": b.thinking, "signature": b.signature})
            elif b.type == "text":
                ac.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                tu_id = b.id
                ac.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        history.append({"role": "assistant", "content": ac})
        if tu_id:
            history.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tu_id,
                            "content": "35",
                        }
                    ],
                }
            )
            last_user = f"OK, now compute {(turn + 1) * 11} + {(turn + 2) * 13}."
        else:
            break
