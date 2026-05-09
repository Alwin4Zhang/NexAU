import pytest

from nexau.core.messages import ImageBlock, Message, Role, TextBlock, ToolResultBlock


def test_message_supports_legacy_dict_style_access_with_deprecation_warning() -> None:
    msg = Message.user("hi")

    with pytest.warns(DeprecationWarning):
        assert msg["role"] == "user"

    with pytest.warns(DeprecationWarning):
        assert msg["content"] == "hi"

    with pytest.warns(DeprecationWarning):
        assert msg.get("content") == "hi"

    with pytest.warns(DeprecationWarning):
        assert msg.get("missing_key", "default") == "default"

    with pytest.warns(DeprecationWarning), pytest.raises(KeyError):
        _ = msg["missing_key"]


def test_tool_message_legacy_access_exposes_tool_call_id_and_content() -> None:
    tool_msg = Message(role=Role.TOOL, content=[ToolResultBlock(tool_use_id="call_1", content="4")])

    with pytest.warns(DeprecationWarning):
        assert tool_msg["role"] == "tool"

    with pytest.warns(DeprecationWarning):
        assert tool_msg["tool_call_id"] == "call_1"

    with pytest.warns(DeprecationWarning):
        assert tool_msg["content"] == "4"


def test_tool_message_with_multimodal_tool_result_folds_images_as_placeholders() -> None:
    # When tool result contains only images, legacy tool-role messages still have text content via "<image>" placeholders.
    tool_msg = Message(
        role=Role.TOOL,
        content=[
            ToolResultBlock(
                tool_use_id="call_1",
                content=[
                    ImageBlock(url="https://example.com/a.png"),
                ],
            )
        ],
    )

    with pytest.warns(DeprecationWarning):
        assert tool_msg["tool_call_id"] == "call_1"

    with pytest.warns(DeprecationWarning):
        assert tool_msg["content"] == "<image>"


def test_tool_message_with_empty_multimodal_tool_result_uses_tool_output_fallback() -> None:
    tool_msg = Message(
        role=Role.TOOL,
        content=[
            ToolResultBlock(
                tool_use_id="call_1",
                content=[],
            )
        ],
    )

    with pytest.warns(DeprecationWarning):
        assert tool_msg["content"] == "<tool_output>"


def test_assistant_legacy_content_with_images_folds_tool_result_blocks_to_text_parts() -> None:
    # If the assistant message contains any images, legacy conversion uses structured `content` parts.
    msg = Message(
        role=Role.ASSISTANT,
        content=[
            TextBlock(text="a"),
            ToolResultBlock(
                tool_use_id="call_1",
                content=[
                    TextBlock(text="x"),
                    ImageBlock(url="https://example.com/tool.png"),
                ],
            ),
            ImageBlock(url="https://example.com/user.png", detail="high"),
        ],
    )

    with pytest.warns(DeprecationWarning):
        content = msg["content"]

    assert isinstance(content, list)
    # ToolResultBlock should have been folded into a text part with "<image>" placeholders.
    assert any(isinstance(p, dict) and p.get("type") == "text" and p.get("text") == "a" for p in content)
    assert any(isinstance(p, dict) and p.get("type") == "text" and p.get("text") == "x<image>" for p in content)
    # And the actual assistant image should be present as an image_url part.
    assert any(isinstance(p, dict) and p.get("type") == "image_url" for p in content)


# ============================================================================
# Legacy ID coercion (UUID → str)
# ============================================================================
#
# Pre-Phase-1 callers constructed Messages with ``id=uuid4()`` directly
# (passing a uuid.UUID instance). Pydantic v2's strict ``str`` validation
# rejects UUID instances by default, so without ``_coerce_id_to_str``
# such code would crash. Lock the coercion in.


def test_message_id_accepts_uuid_object_legacy_caller() -> None:
    """``Message(id=uuid.uuid4())`` must work (pre-Phase-1 SDK pattern).

    ``id`` is annotated ``str`` but the legacy validator coerces UUID at
    parse time. The ``# type: ignore`` is exactly what we're locking in:
    strict callers shouldn't pass UUID, but coercion guards old / loose
    callers that historically did.
    """
    from uuid import UUID, uuid4

    raw = uuid4()
    msg = Message(role=Role.USER, content=[TextBlock(text="hi")], id=raw)  # type: ignore[arg-type]
    assert isinstance(msg.id, str)
    assert UUID(msg.id) == raw  # round-trip parses back to same UUID


def test_message_id_accepts_arbitrary_legacy_string_format() -> None:
    """Non-UUID strings (e.g. ``msg-run-{ts}-{rand}-{seq}-history-{idx}``
    from legacy callers) must pass through unchanged — coercion never
    rewrites already-string IDs."""
    legacy_id = "msg-run-1234567890-abcd-001-history-0"
    msg = Message(role=Role.USER, content=[TextBlock(text="hi")], id=legacy_id)
    assert msg.id == legacy_id


def test_message_id_default_is_uuid_string() -> None:
    """Without explicit ``id=``, default factory generates UUID string."""
    from uuid import UUID

    msg = Message(role=Role.USER, content=[TextBlock(text="hi")])
    UUID(msg.id)  # must parse — would raise if not a UUID-formatted string
