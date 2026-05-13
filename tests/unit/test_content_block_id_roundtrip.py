"""ContentBlock id field roundtrip + back-compat tests.

RFC-0022 §6.4 — TextBlock / ImageBlock / ReasoningBlock get an optional
``id`` field for cross-stream stability between live SSE and persisted
run_actions. This module asserts:

- New code paths CAN populate id and it round-trips through Pydantic
  JSON serialization without loss.
- Old persisted data WITHOUT id loads cleanly with id=None (back-compat).
- Mixed content (one block has id, one doesn't) survives serialization.
"""

from __future__ import annotations

from nexau.core.messages import (
    ImageBlock,
    Message,
    ReasoningBlock,
    Role,
    TextBlock,
    ToolUseBlock,
)


def test_text_block_id_roundtrip() -> None:
    """Newly-emitted TextBlock with id round-trips through JSON."""
    block = TextBlock(id="msg-abc-123", text="hello")
    dumped = block.model_dump()
    assert dumped["id"] == "msg-abc-123"
    assert dumped["text"] == "hello"
    restored = TextBlock.model_validate(dumped)
    assert restored.id == "msg-abc-123"
    assert restored.text == "hello"


def test_text_block_legacy_load_without_id() -> None:
    """Old persisted JSON without ``id`` loads with id=None (back-compat).

    This is the canonical 'old data after migration' shape — pre-RFC-0022
    §6.4 actions stored just ``{"type": "text", "text": "..."}``.
    """
    legacy_json = {"type": "text", "text": "hello"}
    block = TextBlock.model_validate(legacy_json)
    assert block.id is None
    assert block.text == "hello"


def test_image_block_id_roundtrip() -> None:
    block = ImageBlock(id="msg-img-001", url="https://example.com/x.png")
    dumped = block.model_dump()
    assert dumped["id"] == "msg-img-001"
    restored = ImageBlock.model_validate(dumped)
    assert restored.id == "msg-img-001"


def test_image_block_legacy_load_without_id() -> None:
    legacy_json = {"type": "image", "url": "https://example.com/x.png"}
    block = ImageBlock.model_validate(legacy_json)
    assert block.id is None
    assert block.url == "https://example.com/x.png"


def test_reasoning_block_id_roundtrip() -> None:
    block = ReasoningBlock(id="msg-think-001", text="thinking out loud")
    dumped = block.model_dump()
    assert dumped["id"] == "msg-think-001"
    restored = ReasoningBlock.model_validate(dumped)
    assert restored.id == "msg-think-001"


def test_reasoning_block_legacy_load_without_id() -> None:
    legacy_json = {"type": "reasoning", "text": "thinking out loud"}
    block = ReasoningBlock.model_validate(legacy_json)
    assert block.id is None
    assert block.text == "thinking out loud"


def test_tool_use_block_id_unchanged() -> None:
    """``ToolUseBlock.id`` was already required; RFC-0022 §6.4 doesn't touch it."""
    block = ToolUseBlock(id="tu-1", name="calc", input={"expr": "1+1"})
    assert block.id == "tu-1"
    # Required — model_validate without id must fail.
    try:
        ToolUseBlock.model_validate({"type": "tool_use", "name": "calc", "input": {}})
    except Exception:
        return
    raise AssertionError("ToolUseBlock without id should raise validation error")


def test_message_with_mixed_block_ids_roundtrip() -> None:
    """A Message containing blocks with and without ids serializes cleanly.

    Realistic mid-migration state: some blocks were emitted by upgraded
    runtime (have ids), others by older paths (don't). Both must coexist
    in one persisted Message without one corrupting the other.
    """
    msg = Message(
        role=Role.ASSISTANT,
        content=[
            TextBlock(id="msg-1", text="let me check"),
            ToolUseBlock(id="tu-1", name="search", input={"q": "x"}),
            TextBlock(text="here's the answer"),  # legacy-path-emitted, no id
        ],
    )
    dumped = msg.model_dump()
    restored = Message.model_validate(dumped)
    assert len(restored.content) == 3
    block0, block1, block2 = restored.content[0], restored.content[1], restored.content[2]
    # Narrow via isinstance — CLAUDE.md type-safety rules forbid type: ignore.
    assert isinstance(block0, TextBlock) and block0.id == "msg-1"
    assert isinstance(block1, ToolUseBlock) and block1.id == "tu-1"
    assert isinstance(block2, TextBlock) and block2.id is None
