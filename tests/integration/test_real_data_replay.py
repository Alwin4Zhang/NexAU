# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Strict replay validation — synthetic fixtures + cross-key isolation.

These tests run the strict replay machinery from ``replay_oracle.py`` on
synthetic sqlite fixtures. They guard against:

- Caveat 2/4: production load_messages losing data due to ORM/Pydantic
  schema bugs (block dropped, type renamed, field type widened). The oracle
  reads raw JSON and bypasses both layers — any divergence surfaces.
- Caveat 3: REPLACE early-stop in production silently dropping post-anchor
  data. The oracle reads everything; a divergence at any index catches it.
- Caveat 5: cross-(user, session, agent) message-id leak with same-content
  vs different-content classification.

Real-data replay (the 14k-session validation) is a separate exercise — see
``scripts/replay_real_data.py`` for the CLI that ops can run on a prod dump.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from nexau.archs.session.models.agent_run_action_model import AgentRunActionModel
from nexau.archs.session.orm.sql_engine import SQLDatabaseEngine
from nexau.core.messages import Message, Role, TextBlock
from tests.integration.replay_oracle import (
    classify_cross_key_reuse,
    oracle_fold,
    run_replay_sync,
    strict_signature,
)

# ============================================================================
# Helpers
# ============================================================================


def _persist_actions_sync(actions: list[AgentRunActionModel]) -> str:
    """Persist ``actions`` to a fresh sqlite file and return its path.

    Sync (uses ``asyncio.run`` internally) so callers don't have to be inside
    an event loop — necessary because ``run_replay_sync`` also uses
    ``asyncio.run`` and we can't nest those.
    """
    fd, path = tempfile.mkstemp(suffix=".db", prefix="replay_test_")
    os.close(fd)

    async def _write():
        eng = SQLDatabaseEngine.from_url(f"sqlite+aiosqlite:///{path}")
        try:
            await eng.setup_models([AgentRunActionModel])
            for a in actions:
                fresh = AgentRunActionModel(**a.model_dump())
                await eng.create(fresh)
        finally:
            await eng._engine.dispose()

    asyncio.run(_write())
    return path


@asynccontextmanager
async def _persisted_db(actions: list[AgentRunActionModel]) -> AsyncGenerator[str]:
    """Async variant for tests that already drive the loop themselves."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="replay_test_")
    os.close(fd)
    eng = SQLDatabaseEngine.from_url(f"sqlite+aiosqlite:///{path}")
    try:
        await eng.setup_models([AgentRunActionModel])
        for a in actions:
            fresh = AgentRunActionModel(**a.model_dump())
            await eng.create(fresh)
        await eng._engine.dispose()
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)


def _msg(text: str, role: Role = Role.ASSISTANT) -> Message:
    return Message(role=role, content=[TextBlock(text=text)])


_KW = dict(user_id="u", session_id="s", agent_id="a")


# ============================================================================
# Strict signature unit tests — algorithm correctness
# ============================================================================


def test_strict_signature_identical_for_dict_and_message():
    """Same logical message → same signature regardless of input shape."""
    m = Message(id="abc", role=Role.USER, content=[TextBlock(text="hello")])
    m_as_dict = m.model_dump()
    sig_from_msg = strict_signature([m])
    sig_from_dict = strict_signature([m_as_dict])
    assert sig_from_msg == sig_from_dict
    assert sig_from_msg == [("abc", "user", 5, 1, ("text",))]


def test_strict_signature_block_count_dimension():
    """Two messages with different block counts produce different sigs."""
    m1 = Message(id="x", role=Role.ASSISTANT, content=[TextBlock(text="a")])
    m2 = Message(
        id="x",
        role=Role.ASSISTANT,
        content=[TextBlock(text="a"), TextBlock(text="")],
    )
    s1 = strict_signature([m1])
    s2 = strict_signature([m2])
    assert s1 != s2
    assert s1[0][3] == 1 and s2[0][3] == 2  # block_count


def test_strict_signature_text_chars_includes_any_text_field():
    """Block-type-agnostic — any block with .text contributes to text_chars."""
    # Simulate a hypothetical block dict shape with text
    sig = strict_signature(
        [
            {
                "id": "1",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "abc"},
                    {"type": "reasoning", "text": "xyz12"},
                ],
            }
        ]
    )
    assert sig[0] == ("1", "assistant", 8, 2, ("reasoning", "text"))


# ============================================================================
# Oracle fold unit tests — independent of production
# ============================================================================


def _oracle_with_cleanup(actions):
    path = _persist_actions_sync(actions)
    try:
        return oracle_fold(path, ("u", "s", "a"))
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_oracle_fold_pure_append():
    actions = [
        AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("a")], **_KW),
        AgentRunActionModel.create_append(run_id="r2", root_run_id="r2", messages=[_msg("b")], **_KW),
    ]
    msgs = _oracle_with_cleanup(actions)
    assert [m["content"][0]["text"] for m in msgs] == ["a", "b"]


def test_oracle_fold_replace_anchors():
    """Oracle correctly applies REPLACE as full state reset."""
    actions = [
        AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("wiped")], **_KW),
        AgentRunActionModel.create_replace(run_id="r2", root_run_id="r2", messages=[_msg("anchor")], reason="user_clear", **_KW),
        AgentRunActionModel.create_append(run_id="r3", root_run_id="r3", messages=[_msg("kept")], **_KW),
    ]
    msgs = _oracle_with_cleanup(actions)
    assert [m["content"][0]["text"] for m in msgs] == ["anchor", "kept"]


def test_oracle_fold_undo_via_cutoff_ns():
    """Oracle's UNDO handling matches production's cutoff_ns semantics."""
    actions = [
        AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("keep")], **_KW),
        AgentRunActionModel.create_run_start(run_id="r2", root_run_id="r2", **_KW),
        AgentRunActionModel.create_append(run_id="r2", root_run_id="r2", messages=[_msg("undone")], **_KW),
        AgentRunActionModel.create_run_end(run_id="r2", root_run_id="r2", status="ok", **_KW),
        AgentRunActionModel.create_undo(run_id="r_undo", root_run_id="r_undo", undo_before_run_id="r2", **_KW),
    ]
    msgs = _oracle_with_cleanup(actions)
    assert [m["content"][0]["text"] for m in msgs] == ["keep"]


# ============================================================================
# End-to-end strict replay — production vs oracle parity
# ============================================================================


def _replay_with_cleanup(actions: list[AgentRunActionModel]):
    path = _persist_actions_sync(actions)
    try:
        return run_replay_sync(path)
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_replay_clean_session_full_parity():
    actions = [
        AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("a"), _msg("b")], **_KW),
        AgentRunActionModel.create_replace(run_id="r2", root_run_id="r2", messages=[_msg("c")], reason="user_clear", **_KW),
        AgentRunActionModel.create_append(run_id="r3", root_run_id="r3", messages=[_msg("d")], **_KW),
    ]
    result = _replay_with_cleanup(actions)
    assert result.all_clean, result.summarize()
    assert result.total_sessions == 1
    assert result.matches == 1
    assert result.diff_content_reuses == 0


def test_replay_undo_round_trip_parity():
    """UNDO scenario — oracle and production both compute cutoff correctly."""
    actions = [
        AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("keep")], **_KW),
        AgentRunActionModel.create_append(run_id="r2", root_run_id="r2", messages=[_msg("undone")], **_KW),
        AgentRunActionModel.create_undo(run_id="r_undo", root_run_id="r_undo", undo_before_run_id="r2", **_KW),
    ]
    result = _replay_with_cleanup(actions)
    assert result.all_clean, result.summarize()


def test_replay_compaction_class_b_aliasing_parity():
    """REPLACE with reason='compact_*' folds the same as plain REPLACE."""
    actions = [
        AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("old1")], **_KW),
        AgentRunActionModel.create_replace(
            run_id="r2",
            root_run_id="r2",
            messages=[_msg("compacted")],
            reason="compact_auto",
            **_KW,
        ),
        AgentRunActionModel.create_append(run_id="r3", root_run_id="r3", messages=[_msg("new")], **_KW),
    ]
    result = _replay_with_cleanup(actions)
    assert result.all_clean, result.summarize()


# ============================================================================
# Cross-key isolation — classify_cross_key_reuse correctness
# ============================================================================


def test_classify_no_reuse_clean():
    """Independent sessions, no reuse — classify reports nothing."""
    sig_by_key = {
        ("u", "s1", "a"): [("m1", "user", 3, 1, ("text",))],
        ("u", "s2", "a"): [("m2", "user", 3, 1, ("text",))],
    }
    same, diff = classify_cross_key_reuse(sig_by_key)
    assert same == [] and diff == []


def test_classify_legitimate_fork_as_same_content():
    """User forks session: same id, same content → classified same-content (legit)."""
    same_msg_sig = ("forked_id", "assistant", 10, 1, ("text",))
    sig_by_key = {
        ("u", "session_old", "agent_old"): [same_msg_sig],
        ("u", "session_new", "agent_new"): [same_msg_sig],
    }
    same, diff = classify_cross_key_reuse(sig_by_key)
    assert len(same) == 1 and len(diff) == 0
    leaked_id, keys = same[0]
    assert leaked_id == "forked_id"
    assert len(keys) == 2


def test_classify_real_leak_as_different_content():
    """Same id but content differs across keys — REAL BUG (different-content)."""
    sig_by_key = {
        ("u", "s1", "a1"): [("shared_id", "assistant", 10, 1, ("text",))],
        ("u", "s2", "a2"): [("shared_id", "assistant", 50, 2, ("image", "text"))],
    }
    same, diff = classify_cross_key_reuse(sig_by_key)
    assert len(same) == 0 and len(diff) == 1
    leaked_id, keys, content_sigs = diff[0]
    assert leaked_id == "shared_id"
    assert len(content_sigs) == 2  # two distinct content fingerprints


def test_classify_cross_user_reuse_flagged():
    """Same id across DIFFERENT users — extra-suspicious cross-user case."""
    sig_by_key = {
        ("alice", "s1", "a"): [("shared_id", "user", 5, 1, ("text",))],
        ("bob", "s2", "a"): [("shared_id", "user", 5, 1, ("text",))],
    }
    same, diff = classify_cross_key_reuse(sig_by_key)
    # Same content here — classifier puts it in same-content. Caller (the
    # CLI / replay summary) is responsible for noticing the cross-user span.
    assert len(same) == 1
    _, keys = same[0]
    user_count = len({k[0] for k in keys})
    assert user_count == 2  # the cross-user-ness is observable in keys[0]


# ============================================================================
# Pydantic schema integrity — the key Caveat-2/4 regression guard
# ============================================================================


def test_oracle_independent_of_production_pydantic():
    """If production's Pydantic Message schema added a NEW required field
    that the oracle doesn't know about, the oracle still works (it only
    looks at id / role / content / type / text fields it cares about).

    Conversely, if production silently dropped a content block during
    Pydantic parsing, the oracle would still see it in the raw JSON and
    diverge — that's the regression this oracle exists to catch.
    """
    # Use sync persistence to avoid nesting asyncio.run with run_replay_sync
    db_path = _persist_actions_sync([])
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO agent_run_actions ("
            "  action_id, user_id, session_id, agent_id, run_id, root_run_id, "
            "  agent_name, created_at, created_at_ns, action_type, append_messages"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "act_oracle_test",
                "u",
                "s",
                "a",
                "r1",
                "r1",
                "test",
                "2026-05-05 00:00:00",
                1000,
                "append",
                json.dumps(
                    [
                        {
                            "id": "msg1",
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "hi"},
                                {"type": "made_up_block", "text": "future block"},
                            ],
                        }
                    ]
                ),
            ),
        )
        conn.commit()
        conn.close()
        msgs = oracle_fold(db_path, ("u", "s", "a"))
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)
    assert len(msgs) == 1
    assert len(msgs[0]["content"]) == 2  # both blocks visible to oracle
    assert {b["type"] for b in msgs[0]["content"]} == {"text", "made_up_block"}


# ============================================================================
# all_clean semantics
# ============================================================================


def test_all_clean_property_with_diff_content_reuse_signals_dirty():
    """Even with full oracle parity, diff_content_reuses>0 means NOT clean."""
    from tests.integration.replay_oracle import ReplayResult

    r = ReplayResult()
    r.total_sessions = 5
    r.matches = 5
    r.diff_content_reuses = 1
    assert not r.all_clean


def test_all_clean_property_with_only_same_content_reuse_is_clean():
    """Same-content reuse (legit fork) doesn't make the result dirty."""
    from tests.integration.replay_oracle import ReplayResult

    r = ReplayResult()
    r.total_sessions = 5
    r.matches = 5
    r.same_content_reuses = 3  # forks
    r.diff_content_reuses = 0
    assert r.all_clean


def test_all_clean_property_with_errors_is_dirty():
    from tests.integration.replay_oracle import ReplayResult

    r = ReplayResult()
    r.total_sessions = 5
    r.matches = 4  # one errored
    r.errors.append("crashed")
    assert not r.all_clean


# ============================================================================
# Smoke: don't accidentally collect the helper module as tests
# ============================================================================


def test_replay_oracle_helper_not_collected_as_tests():
    """Sanity: the helper module's filename must not start with test_."""
    import os.path as _p

    from tests.integration import replay_oracle

    assert not _p.basename(replay_oracle.__file__).startswith("test_")
