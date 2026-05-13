# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""ReplaceExtra discriminated-union edge cases (RFC-0022 §6.3).

This file is the dedicated regression battery for the protobuf-oneof-equivalent
``ReplaceExtra`` design. Each test pins one specific behavior; together they
target full branch coverage of the discriminator + all typed factories +
forward-compat fallback.

Categories:
- Discriminator dispatch (dict input + BaseModel input + missing reason)
- Each variant's construction + validation + round-trip
- Each typed factory's contract (required vs optional fields, error paths)
- Generic ``create_replace`` dispatch behavior across all reason values
- Forward-compat: unknown reason falls to UnknownReplaceVariant; unknown
  fields preserved verbatim across known variants
- ``parse_extra`` paths for REPLACE (variant dispatch) + non-REPLACE +
  unknown action_type (forward-compat skip)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexau.archs.session.models.agent_run_action_model import (
    _REPLACE_EXTRA_ADAPTER,
    AgentRunActionModel,
    AppendExtra,
    CompactAutoVariant,
    CompactFocusedVariant,
    CompactManualVariant,
    CompactStats,
    RunActionType,
    UndoExtra,
    UnknownReplaceVariant,
    UserClearVariant,
    _discriminate_replace,
)
from nexau.core.messages import Message, Role, TextBlock


def _msg(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=[TextBlock(text=text)])


_KW = dict(user_id="u", session_id="s", agent_id="a", run_id="r1", root_run_id="r1")


# ============================================================================
# Discriminator: dict and BaseModel inputs both work, missing reason → unknown
# ============================================================================


@pytest.mark.parametrize(
    "reason, expected_tag",
    [
        ("user_clear", "user_clear"),
        ("compact_auto", "compact_auto"),
        ("compact_manual", "compact_manual"),
        ("compact_focused", "compact_focused"),
        ("unknown_future_reason_v9", "__unknown__"),
        ("", "__unknown__"),  # empty string ≠ any known
        (None, "__unknown__"),  # missing reason
    ],
)
def test_discriminator_dispatches_dict_input(reason, expected_tag):
    payload = {} if reason is None else {"reason": reason}
    assert _discriminate_replace(payload) == expected_tag


def test_discriminator_dispatches_basemodel_input():
    """Discriminator must work on Pydantic instances too (re-validation path)."""
    instance = UserClearVariant()
    assert _discriminate_replace(instance) == "user_clear"

    focused = CompactFocusedVariant(focus_instructions="X")
    assert _discriminate_replace(focused) == "compact_focused"


def test_discriminator_handles_object_without_reason_attr():
    """Plain objects with no ``reason`` attribute fall to __unknown__."""

    class _Bare:
        pass

    assert _discriminate_replace(_Bare()) == "__unknown__"


# ============================================================================
# Variant validation: required vs optional fields
# ============================================================================


def test_user_clear_variant_constructible_with_no_args():
    v = UserClearVariant()
    assert v.reason == "user_clear"
    assert v.trace_id is None


def test_compact_auto_variant_optional_fields():
    v = CompactAutoVariant()
    assert v.reason == "compact_auto"
    assert v.strategy is None
    assert v.stats is None


def test_compact_manual_variant_optional_fields():
    v = CompactManualVariant()
    assert v.reason == "compact_manual"
    assert v.strategy is None
    assert v.stats is None


def test_compact_focused_variant_requires_focus_instructions():
    """Required at construction time — the whole point of this variant."""
    with pytest.raises(ValidationError):
        CompactFocusedVariant()  # type: ignore[call-arg]
    # With focus_instructions: ok
    v = CompactFocusedVariant(focus_instructions="keep auth")
    assert v.focus_instructions == "keep auth"


def test_unknown_replace_variant_requires_reason():
    """UnknownReplaceVariant.reason has no default — must be supplied."""
    with pytest.raises(ValidationError):
        UnknownReplaceVariant()  # type: ignore[call-arg]
    v = UnknownReplaceVariant(reason="future_v3")
    assert v.reason == "future_v3"


# ============================================================================
# Adapter dispatch — known + unknown reasons → correct variant types
# ============================================================================


def test_adapter_dispatches_user_clear():
    v = _REPLACE_EXTRA_ADAPTER.validate_python({"reason": "user_clear"})
    assert isinstance(v, UserClearVariant)


def test_adapter_dispatches_compact_auto():
    v = _REPLACE_EXTRA_ADAPTER.validate_python({"reason": "compact_auto", "strategy": "sliding_window"})
    assert isinstance(v, CompactAutoVariant)
    assert v.strategy == "sliding_window"


def test_adapter_dispatches_compact_manual():
    v = _REPLACE_EXTRA_ADAPTER.validate_python({"reason": "compact_manual"})
    assert isinstance(v, CompactManualVariant)


def test_adapter_dispatches_compact_focused():
    v = _REPLACE_EXTRA_ADAPTER.validate_python({"reason": "compact_focused", "focus_instructions": "Y"})
    assert isinstance(v, CompactFocusedVariant)
    assert v.focus_instructions == "Y"


def test_adapter_dispatches_compact_focused_missing_field_raises():
    """Even via adapter dispatch, focus_instructions stays REQUIRED."""
    with pytest.raises(ValidationError, match="focus_instructions"):
        _REPLACE_EXTRA_ADAPTER.validate_python({"reason": "compact_focused"})


def test_adapter_dispatches_unknown_to_fallback():
    v = _REPLACE_EXTRA_ADAPTER.validate_python({"reason": "fork"})
    assert isinstance(v, UnknownReplaceVariant)
    assert v.reason == "fork"


def test_adapter_unknown_reason_preserves_arbitrary_extra_fields():
    """protobuf oneof unknown-field rule: preserve raw payload across roundtrip."""
    payload = {
        "reason": "fork",
        "source_user_id": "alice",
        "source_session_id": "sess_1",
        "source_run_id": "run_42",
        "nested": {"deep": {"value": 99}},
    }
    v = _REPLACE_EXTRA_ADAPTER.validate_python(payload)
    assert isinstance(v, UnknownReplaceVariant)
    dumped = v.model_dump()
    for k, expected in payload.items():
        assert dumped[k] == expected, f"field {k!r} not preserved: {dumped.get(k)!r} != {expected!r}"


def test_adapter_known_variant_preserves_unknown_fields():
    """``extra='allow'`` on known variant base too — old reader, future field added."""
    payload = {"reason": "compact_auto", "strategy": "x", "future_telemetry": {"latency_ms": 17}}
    v = _REPLACE_EXTRA_ADAPTER.validate_python(payload)
    assert isinstance(v, CompactAutoVariant)
    dumped = v.model_dump()
    assert dumped["future_telemetry"] == {"latency_ms": 17}


# ============================================================================
# model_dump → validate roundtrip preserves variant type identity
# ============================================================================


@pytest.mark.parametrize(
    "constructor",
    [
        lambda: UserClearVariant(trace_id="t1"),
        lambda: CompactAutoVariant(strategy="sliding_window", stats=CompactStats(pre_tokens=100)),
        lambda: CompactManualVariant(strategy="tool_result"),
        lambda: CompactFocusedVariant(focus_instructions="keep X", strategy="manual"),
        lambda: UnknownReplaceVariant(reason="fork", trace_id="t2"),
    ],
)
def test_variant_dump_then_validate_roundtrip(constructor):
    """model_dump → validate_python returns same variant type with same fields."""
    original = constructor()
    dumped = original.model_dump(exclude_none=True)
    rebuilt = _REPLACE_EXTRA_ADAPTER.validate_python(dumped)
    assert type(rebuilt) is type(original)
    assert rebuilt.model_dump(exclude_none=True) == dumped


# ============================================================================
# Typed factory contracts
# ============================================================================


def test_factory_create_replace_user_clear():
    row = AgentRunActionModel.create_replace_user_clear(messages=[_msg("clean")], trace_id="trace_1", **_KW)
    assert row.action_type == RunActionType.REPLACE
    assert row.replace_messages is not None and len(row.replace_messages) == 1
    extra = row.parse_extra()
    assert isinstance(extra, UserClearVariant)
    assert extra.trace_id == "trace_1"


def test_factory_create_replace_compact_auto():
    row = AgentRunActionModel.create_replace_compact_auto(
        messages=[_msg("summary")],
        strategy="sliding_window",
        stats=CompactStats(pre_message_count=10, post_message_count=2),
        **_KW,
    )
    extra = row.parse_extra()
    assert isinstance(extra, CompactAutoVariant)
    assert extra.strategy == "sliding_window"
    assert extra.stats is not None and extra.stats.pre_message_count == 10


def test_factory_create_replace_compact_manual():
    row = AgentRunActionModel.create_replace_compact_manual(messages=[_msg("summary")], strategy="tool_result", **_KW)
    extra = row.parse_extra()
    assert isinstance(extra, CompactManualVariant)
    assert extra.strategy == "tool_result"


def test_factory_create_replace_compact_focused():
    row = AgentRunActionModel.create_replace_compact_focused(
        messages=[_msg("summary")],
        focus_instructions="Keep auth-related discussion",
        strategy="user_model_full_trace",
        stats=CompactStats(pre_tokens=50000, post_tokens=8000),
        **_KW,
    )
    extra = row.parse_extra()
    assert isinstance(extra, CompactFocusedVariant)
    assert extra.focus_instructions == "Keep auth-related discussion"
    assert extra.stats is not None and extra.stats.pre_tokens == 50000


def test_factory_compact_focused_rejects_empty_string():
    """Empty focus_instructions defeats the variant's purpose — factory rejects."""
    with pytest.raises(ValueError, match="focus_instructions"):
        AgentRunActionModel.create_replace_compact_focused(messages=[_msg("x")], focus_instructions="", **_KW)


def test_factory_compact_focused_rejects_none():
    """``None`` (Python type error at sig level + runtime guard belt-and-suspenders)."""
    with pytest.raises((ValueError, TypeError)):
        AgentRunActionModel.create_replace_compact_focused(
            messages=[_msg("x")],
            focus_instructions=None,  # type: ignore[arg-type]
            **_KW,
        )


# ============================================================================
# Generic create_replace dispatch — covers all reason values
# ============================================================================


def test_generic_create_replace_no_reason_writes_no_extra():
    """``persist_replace`` from history_list.py path — no reason, no extra."""
    row = AgentRunActionModel.create_replace(messages=[_msg("x")], **_KW)
    assert row.extra is None
    assert row.parse_extra() is None


def test_generic_create_replace_user_clear_dispatches_to_variant():
    row = AgentRunActionModel.create_replace(messages=[_msg("x")], reason="user_clear", **_KW)
    extra = row.parse_extra()
    assert isinstance(extra, UserClearVariant)


def test_generic_create_replace_compact_auto_dispatches_to_variant():
    row = AgentRunActionModel.create_replace(messages=[_msg("x")], reason="compact_auto", **_KW)
    extra = row.parse_extra()
    assert isinstance(extra, CompactAutoVariant)


def test_generic_create_replace_unknown_reason_dispatches_to_unknown_variant():
    row = AgentRunActionModel.create_replace(messages=[_msg("x")], reason="future_reason_v9", **_KW)
    extra = row.parse_extra()
    assert isinstance(extra, UnknownReplaceVariant)
    assert extra.reason == "future_reason_v9"


def test_generic_create_replace_compact_focused_without_field_fails():
    """Generic factory routes through dispatcher; missing required field raises."""
    with pytest.raises(ValidationError, match="focus_instructions"):
        AgentRunActionModel.create_replace(messages=[_msg("x")], reason="compact_focused", **_KW)


# ============================================================================
# parse_extra() coverage — all action_type branches incl. unknown
# ============================================================================


def test_parse_extra_returns_none_for_unknown_action_type():
    """Forward-compat: row written by future SDK with unknown action_type
    must not crash. parse_extra returns None so callers handle gracefully."""
    model = AgentRunActionModel(
        user_id="u",
        session_id="s",
        agent_id="a",
        run_id="r1",
        root_run_id="r1",
        action_type="future_unknown_action_type_v3",  # not in RunActionType
        extra={"reason": "user_clear"},
    )
    assert model.parse_extra() is None


def test_parse_extra_returns_none_when_extra_is_none():
    """No extra column → parse_extra returns None (every action_type)."""
    for at in RunActionType:
        model = AgentRunActionModel(
            user_id="u",
            session_id="s",
            agent_id="a",
            run_id="r1",
            root_run_id="r1",
            action_type=at,
            extra=None,
        )
        assert model.parse_extra() is None, f"{at} with extra=None should give None"


def test_parse_extra_replace_with_legacy_extra_no_reason_falls_to_unknown():
    """Legacy extra dict on REPLACE without reason → UnknownReplaceVariant
    requires reason, so this raises. Documents the edge case."""
    model = AgentRunActionModel(
        user_id="u",
        session_id="s",
        agent_id="a",
        run_id="r1",
        root_run_id="r1",
        action_type=RunActionType.REPLACE,
        extra={"some_legacy_field": "x"},  # no reason at all
    )
    # UnknownReplaceVariant requires reason — strict failure rather than silent
    # pass, so legacy malformed rows surface loudly.
    with pytest.raises(ValidationError):
        model.parse_extra()


# ============================================================================
# Pattern-matching demonstration — verify variant types narrow as expected
# ============================================================================


def test_pattern_matching_user_clear_branch_narrowed():
    extra = _REPLACE_EXTRA_ADAPTER.validate_python({"reason": "user_clear"})
    branch_taken: str | None = None
    match extra:
        case UserClearVariant():
            branch_taken = "user_clear"
        case CompactAutoVariant():
            branch_taken = "compact_auto"
        case CompactFocusedVariant():
            branch_taken = "compact_focused"
        case _:
            branch_taken = "other"
    assert branch_taken == "user_clear"


def test_pattern_matching_compact_focused_field_access():
    """Pattern match exposes focus_instructions only on the right branch."""
    extra = _REPLACE_EXTRA_ADAPTER.validate_python({"reason": "compact_focused", "focus_instructions": "auth context"})
    fi: str | None = None
    match extra:
        case CompactFocusedVariant(focus_instructions=focus):
            fi = focus
        case _:
            fi = None
    assert fi == "auth context"


def test_pattern_matching_unknown_variant_branch_for_forward_compat():
    """Future reason value lands on UnknownReplaceVariant — consumers can log + skip."""
    extra = _REPLACE_EXTRA_ADAPTER.validate_python({"reason": "fork", "source_session_id": "abc"})
    captured_reason: str | None = None
    match extra:
        case UnknownReplaceVariant(reason=r):
            captured_reason = r
        case _:
            captured_reason = None
    assert captured_reason == "fork"


# ============================================================================
# UndoExtra (kept flat) — coverage for free-form reason
# ============================================================================


def test_undo_extra_free_form_reason():
    """UndoExtra.reason is free-form str (no Literal constraint)."""
    e = UndoExtra(reason="user_rewind")
    assert e.reason == "user_rewind"
    e2 = UndoExtra(reason="some_future_reason_we_havent_thought_of")
    assert e2.reason == "some_future_reason_we_havent_thought_of"


def test_create_undo_with_arbitrary_reason():
    """create_undo accepts any string for reason (no Literal enforced)."""
    row = AgentRunActionModel.create_undo(
        user_id="u",
        session_id="s",
        agent_id="a",
        run_id="r_undo",
        root_run_id="r_undo",
        undo_before_run_id="r_target",
        reason="future_undo_kind",
    )
    extra = row.parse_extra()
    assert isinstance(extra, UndoExtra)
    assert extra.reason == "future_undo_kind"


# ============================================================================
# Smoke: AppendExtra still works (regression — variant union didn't break it)
# ============================================================================


def test_append_extra_unchanged_by_replace_union_refactor():
    e = AppendExtra(iter_index=3, trace_id="trace-xyz")
    assert e.iter_index == 3
    assert e.trace_id == "trace-xyz"
