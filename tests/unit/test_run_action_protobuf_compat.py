# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""protobuf-philosophy compatibility tests for *Extra Pydantic classes (RFC-0022).

Verifies the schema evolution guarantees we adopted from protobuf:

1. **All fields optional** — empty dict / missing fields → None default, never raises
2. **Forward compat** (old reader, new data) — unknown future field → ignored, not raised
3. **Backward compat** (new reader, old data) — missing canonical field → None default
4. **enum values are open** — body uses ``str``, unknown enum value (e.g. new
   ``status="degraded"``) round-trips through old reader without ValidationError

These tests are the algebraic guard for "we shipped protobuf evolution
discipline without using protobuf wire format" — see RFC-0022 §设计原则 §3.
"""

from __future__ import annotations

import pytest

from nexau.archs.session.models.agent_run_action_model import (
    _REPLACE_EXTRA_ADAPTER,
    AppendExtra,
    CompactAutoVariant,
    CompactFocusedVariant,
    CompactManualVariant,
    CompactStats,
    RunEndExtra,
    RunStartExtra,
    UndoExtra,
    UnknownReplaceVariant,
    UserClearVariant,
)

# Flat *Extra classes — protobuf philosophy applies (all-optional + extra='allow')
_FLAT_EXTRA_CLASSES = [
    AppendExtra,
    UndoExtra,
    RunStartExtra,
    RunEndExtra,
    CompactStats,
]

# ReplaceExtra is a discriminated union — its variants follow the same
# protobuf philosophy individually (extra='allow' on each variant base).
_REPLACE_VARIANT_CLASSES = [
    UserClearVariant,
    CompactAutoVariant,
    CompactManualVariant,
    UnknownReplaceVariant,
    # CompactFocusedVariant has a REQUIRED field (focus_instructions), so the
    # "empty dict validates to all None" property test doesn't apply to it.
    # Covered separately below.
]

_ALL_EXTRA_CLASSES = _FLAT_EXTRA_CLASSES + [
    UserClearVariant,
    CompactAutoVariant,
    CompactManualVariant,
    # UnknownReplaceVariant has REQUIRED ``reason``, excluded from "all-None" test
]


@pytest.mark.parametrize("extra_cls", _ALL_EXTRA_CLASSES)
def test_empty_dict_validates_to_all_none(extra_cls):
    """Empty payload {} must validate (no required fields at protocol level).

    Discriminator fields (e.g. ``reason: Literal["user_clear"] = "user_clear"``)
    legitimately have non-None defaults — they're the variant tag. All OTHER
    fields must default to None per protobuf philosophy.
    """
    instance = extra_cls.model_validate({})
    # Skip discriminator fields whose default is the variant tag itself
    skip_fields = {"reason"} if extra_cls.__name__.endswith("Variant") else set()
    for field_name, field_info in extra_cls.model_fields.items():
        if field_name in skip_fields:
            continue
        assert getattr(instance, field_name) is None, (
            f"{extra_cls.__name__}.{field_name} should default None on empty dict, got {getattr(instance, field_name)!r}"
        )


@pytest.mark.parametrize("extra_cls", _ALL_EXTRA_CLASSES)
def test_unknown_future_field_does_not_raise(extra_cls):
    """A field not declared in the *Extra class must be tolerated (extra='allow').

    Forward compat: newer SDK writes ``extra={"unknown_future_field": "x"}``;
    older SDK reading this row must not crash.
    """
    payload = {"unknown_future_field_xyz": "future_value"}
    # Should not raise
    instance = extra_cls.model_validate(payload)
    # The unknown field is preserved on the instance per ConfigDict(extra='allow')
    assert "unknown_future_field_xyz" in instance.model_dump()


@pytest.mark.parametrize("extra_cls", _ALL_EXTRA_CLASSES)
def test_partial_field_set_validates(extra_cls):
    """Setting a subset of fields works (no required-field crash)."""
    # Build a minimal payload with just one None-default field overridden
    fields = list(extra_cls.model_fields.keys())
    if not fields:
        pytest.skip("class has no fields")
    # Try with each individual field set, others None
    for field_name in fields:
        # Pick a sentinel value compatible with the field type
        annotation = extra_cls.model_fields[field_name].annotation
        sentinel: object
        # Strip "| None" from annotation by checking for primitives
        if annotation in (str | None, str):
            sentinel = "test"
        elif annotation in (int | None, int):
            sentinel = 42
        elif annotation in (bool | None, bool):
            sentinel = True
        else:
            # Skip complex types (list / nested model)
            continue
        instance = extra_cls.model_validate({field_name: sentinel})
        assert getattr(instance, field_name) == sentinel


def test_run_end_extra_unknown_status_does_not_raise():
    """canonical str fields accept unknown values (e.g. future ``status="degraded"``).

    The ``status`` field documents canonical values "ok" / "error" / "cancelled",
    but at the protocol level it's a plain ``str | None`` so future additions
    don't break old readers. Strict validation lives at the factory layer.
    """
    instance = RunEndExtra.model_validate({"status": "degraded"})
    assert instance.status == "degraded"


def test_replace_extra_unknown_reason_falls_to_unknown_variant():
    """Future ``reason`` values land on UnknownReplaceVariant (protobuf oneof unknown-field rule).

    Critical regression: removing the ``UnknownReplaceVariant`` fallback (or
    its ``__unknown__`` Tag in the discriminated union) makes any new reason
    a Class C silent-corruption hazard (RFC-0022 §设计原则 §6).
    """
    instance = _REPLACE_EXTRA_ADAPTER.validate_python({"reason": "compact_user_premium_v2"})
    assert isinstance(instance, UnknownReplaceVariant)
    assert instance.reason == "compact_user_premium_v2"


def test_replace_extra_known_reasons_dispatch_to_typed_variants():
    """Known reason values dispatch to their specific variants — type-narrowed access."""
    cases = [
        ({"reason": "user_clear"}, UserClearVariant),
        ({"reason": "compact_auto", "strategy": "sliding_window"}, CompactAutoVariant),
        ({"reason": "compact_manual"}, CompactManualVariant),
        ({"reason": "compact_focused", "focus_instructions": "X"}, CompactFocusedVariant),
    ]
    for payload, expected_cls in cases:
        instance = _REPLACE_EXTRA_ADAPTER.validate_python(payload)
        assert isinstance(instance, expected_cls), (
            f"reason={payload['reason']!r} dispatched to {type(instance).__name__}, expected {expected_cls.__name__}"
        )


def test_replace_extra_compact_focused_requires_focus_instructions():
    """CompactFocusedVariant — focus_instructions is REQUIRED (the whole point of the variant)."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="focus_instructions"):
        _REPLACE_EXTRA_ADAPTER.validate_python({"reason": "compact_focused"})


def test_replace_extra_unknown_variant_preserves_extra_fields():
    """UnknownReplaceVariant preserves all unrecognized fields verbatim (extra='allow').

    Mirrors protobuf wire format's "unknown field preservation" guarantee —
    a future SDK adds ``source_session_id`` to a new ``"fork"`` reason; old
    SDK roundtrip-preserves it for audit / re-write.
    """
    payload = {"reason": "fork", "source_session_id": "sess_123", "source_user_id": "alice"}
    instance = _REPLACE_EXTRA_ADAPTER.validate_python(payload)
    assert isinstance(instance, UnknownReplaceVariant)
    dumped = instance.model_dump()
    assert dumped["source_session_id"] == "sess_123"
    assert dumped["source_user_id"] == "alice"


def test_round_trip_preserves_unknown_fields():
    """Old reader reads new data → re-serializes → preserves unknown fields verbatim.

    This is the "old SDK proxy" scenario: a Phase 1 SDK reading a Phase 2
    record with new fields, re-saving (e.g. for audit), must NOT drop the
    new fields.
    """
    payload = {
        "status": "ok",
        "finished_at_ns": 1234567890,
        "future_metric_v2": {"latency_p99_ms": 850, "tokens_used": 5000},
    }
    instance = RunEndExtra.model_validate(payload)
    dumped = instance.model_dump()
    assert dumped["future_metric_v2"] == {"latency_p99_ms": 850, "tokens_used": 5000}
    # Round-trip back through validate
    re_validated = RunEndExtra.model_validate(dumped)
    assert re_validated.model_dump()["future_metric_v2"] == payload["future_metric_v2"]


def test_replace_extra_nested_stats_protobuf_compat():
    """Nested ``CompactStats`` follows protobuf philosophy (all-optional + extra='allow')."""
    # Empty nested
    instance = _REPLACE_EXTRA_ADAPTER.validate_python({"reason": "compact_auto", "stats": {}})
    assert isinstance(instance, CompactAutoVariant)
    assert instance.stats is not None
    assert instance.stats.pre_message_count is None

    # Unknown nested field
    instance2 = _REPLACE_EXTRA_ADAPTER.validate_python({"reason": "compact_auto", "stats": {"unknown_nested": 99}})
    assert isinstance(instance2, CompactAutoVariant)
    assert instance2.stats is not None
    assert "unknown_nested" in instance2.stats.model_dump()
