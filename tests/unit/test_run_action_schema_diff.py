# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Schema-diff CI gate for AgentRunActionModel + *Extra classes (RFC-0022).

Replaces protobuf ``buf`` breaking-change detection. Maintains a frozen
"shipped schema" snapshot in this file; any PR that:

- removes a field (column or *Extra field)
- renames a field (= remove + add — caught as remove)
- changes a field's type from compatible to incompatible (e.g. str → int)

…must update the snapshot AND have a justification in the PR description.

Adding new fields is non-breaking and should pass without snapshot updates
(the test allows superset additions).

When this test fails, the failure message tells you exactly what changed
and asks "is this intentional? if yes, update FROZEN_SCHEMA below."
"""

from __future__ import annotations

import pytest

from nexau.archs.session.models.agent_run_action_model import (
    AgentRunActionModel,
    AppendExtra,
    CompactAutoVariant,
    CompactFocusedVariant,
    CompactManualVariant,
    CompactStats,
    RunActionType,
    RunEndExtra,
    RunStartExtra,
    UndoExtra,
    UnknownReplaceVariant,
    UserClearVariant,
)

# ============================================================================
# FROZEN SHIPPED SCHEMA — update with care, only after PR review
# ============================================================================
#
# Format: {ClassName: {field_name: type_str}}
# type_str is the str() of the annotation (lenient comparison).
#
# Removing a field from this dict (= field deleted from class) → fail
# Adding a field that's not in this dict (= new class field) → pass (additive)
# Type mismatch on existing field → fail (potentially breaking)

FROZEN_SCHEMA: dict[str, dict[str, str]] = {
    "AgentRunActionModel": {
        # primary key + identity
        "action_id": "str",
        "user_id": "str",
        "session_id": "str",
        "agent_id": "str",
        "run_id": "str",
        # run tracking
        "root_run_id": "str",
        "parent_run_id": "str | None",
        "agent_name": "str",
        # timestamps
        "created_at": "datetime",
        "created_at_ns": "int",
        # action discriminator (str at column for forward-compat, see model)
        "action_type": "str",
        # per-action_type payloads
        "append_messages": "list[Message] | None",
        "replace_messages": "list[Message] | None",
        "undo_before_run_id": "str | None",
        # streaming idempotency
        "idempotency_key": "str | None",
        # typed extra (loose dict at column level, *Extra at write/read)
        "extra": "dict[str, Any] | None",
    },
    "AppendExtra": {
        "iter_index": "int | None",
        "trace_id": "str | None",
    },
    # ReplaceExtra is now a discriminated union over per-reason variants.
    # Each variant frozen separately so we catch any field drop / rename per
    # variant. UnknownReplaceVariant is the protobuf-oneof unknown-field fallback.
    "UserClearVariant": {
        "trace_id": "str | None",
        "reason": "Literal['user_clear']",
    },
    "CompactAutoVariant": {
        "trace_id": "str | None",
        "reason": "Literal['compact_auto']",
        "strategy": "str | None",
        "stats": "CompactStats | None",
    },
    "CompactManualVariant": {
        "trace_id": "str | None",
        "reason": "Literal['compact_manual']",
        "strategy": "str | None",
        "stats": "CompactStats | None",
    },
    "CompactFocusedVariant": {
        "trace_id": "str | None",
        "reason": "Literal['compact_focused']",
        "strategy": "str | None",
        # Required: the whole point of this variant is preserving user intent.
        "focus_instructions": "str",
        "stats": "CompactStats | None",
    },
    "UnknownReplaceVariant": {
        "trace_id": "str | None",
        "reason": "str",  # any string the SDK doesn't recognize
    },
    "UndoExtra": {
        "reason": "str | None",
        "trace_id": "str | None",
    },
    "RunStartExtra": {
        "trace_id": "str | None",
    },
    "RunEndExtra": {
        "status": "str | None",
        "finished_at_ns": "int | None",
        "reason": "str | None",
        "trace_id": "str | None",
    },
    "CompactStats": {
        "pre_message_count": "int | None",
        "post_message_count": "int | None",
        "pre_tokens": "int | None",
        "post_tokens": "int | None",
    },
}

# Frozen RunActionType members — adding new members is non-breaking,
# removing or renaming is breaking.
#
# NOTE: ``COMPACT`` was drafted in early Phase 1 design but reclassified as
# Class B (REPLACE + ReplaceExtra(reason='compact_*')) before merge — see
# RFC-0022 §设计原则 §6 — so it never shipped as an enum member.
FROZEN_RUN_ACTION_TYPES: set[str] = {
    "APPEND",
    "UNDO",
    "REPLACE",
    "RUN_START",
    "RUN_END",
}


def _normalize_annotation(annotation) -> str:
    """Render a type annotation as a stable string for comparison.

    Strips module prefixes (``nexau.archs.session.models....CompactStats`` →
    ``CompactStats``) and ``typing.`` prefix so frozen annotations stay
    portable across module reorgs.
    """
    import re

    s = str(annotation)
    # Pydantic / Python sometimes prints "<class 'str'>" — normalize to "str"
    if s.startswith("<class '") and s.endswith("'>"):
        s = s[8:-2]
    # ``<enum 'RunActionType'>`` → "RunActionType"
    if s.startswith("<enum '") and s.endswith("'>"):
        s = s[7:-2]
    # Strip ``typing.`` prefix
    s = s.replace("typing.", "")
    # Strip module path: ``foo.bar.Baz`` → ``Baz`` (keep the leaf)
    s = re.sub(r"\b([a-zA-Z_]\w*\.)+([A-Z][a-zA-Z_]\w*)", r"\2", s)
    # ``datetime.datetime`` → ``datetime`` (lowercase module + same-named class)
    s = s.replace("datetime.datetime", "datetime")
    return s


_CLASSES_UNDER_TEST = {
    "AgentRunActionModel": AgentRunActionModel,
    "AppendExtra": AppendExtra,
    "UndoExtra": UndoExtra,
    "RunStartExtra": RunStartExtra,
    "RunEndExtra": RunEndExtra,
    "CompactStats": CompactStats,
    # ReplaceExtra union variants (each frozen individually)
    "UserClearVariant": UserClearVariant,
    "CompactAutoVariant": CompactAutoVariant,
    "CompactManualVariant": CompactManualVariant,
    "CompactFocusedVariant": CompactFocusedVariant,
    "UnknownReplaceVariant": UnknownReplaceVariant,
}


@pytest.mark.parametrize("class_name", FROZEN_SCHEMA.keys())
def test_frozen_field_still_present_and_compatible(class_name):
    """Every frozen field must still exist with a compatible type.

    Removing a frozen field or changing its type to an incompatible one
    is a breaking change — fail and ask the author to update FROZEN_SCHEMA
    with explicit acknowledgment.
    """
    frozen_fields = FROZEN_SCHEMA[class_name]
    cls = _CLASSES_UNDER_TEST[class_name]
    current_fields = {name: _normalize_annotation(field.annotation) for name, field in cls.model_fields.items()}

    missing = []
    type_changed = []
    for fname, expected_type in frozen_fields.items():
        if fname not in current_fields:
            missing.append(fname)
        elif _normalize_annotation(expected_type) != current_fields[fname]:
            type_changed.append(f"  {fname}: frozen={expected_type!r} current={current_fields[fname]!r}")

    if missing or type_changed:
        msg = f"BREAKING SCHEMA CHANGE in {class_name}:\n"
        if missing:
            msg += f"  Removed fields: {missing}\n"
        if type_changed:
            msg += "  Type-changed fields:\n" + "\n".join(type_changed) + "\n"
        msg += (
            "\nIf intentional, update FROZEN_SCHEMA in this test file to reflect "
            "the new shape. Justify the breaking change in the PR description."
        )
        pytest.fail(msg)


def test_run_action_type_members_not_removed():
    """Removing a RunActionType enum member breaks downstream consumers."""
    current = {member.name for member in RunActionType}
    removed = FROZEN_RUN_ACTION_TYPES - current
    if removed:
        pytest.fail(f"BREAKING: RunActionType members removed: {removed}. Update FROZEN_RUN_ACTION_TYPES if intentional + justify.")


@pytest.mark.parametrize("class_name", FROZEN_SCHEMA.keys())
def test_additive_changes_are_allowed(class_name):
    """Adding new fields not in FROZEN_SCHEMA is non-breaking — should pass.

    This test documents the policy: additive evolution is encouraged.
    No assertion needed; purpose is to make the policy visible alongside
    the breaking-change check.
    """
    # Just verify the class still imports + has at least the frozen fields
    cls = _CLASSES_UNDER_TEST[class_name]
    current = set(cls.model_fields.keys())
    frozen = set(FROZEN_SCHEMA[class_name].keys())
    new_additive = current - frozen
    if new_additive:
        # Informational only — not a failure
        print(f"\n[INFO] {class_name} added new fields (additive, non-breaking): {new_additive}")
