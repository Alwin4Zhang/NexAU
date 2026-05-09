# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Production fold algorithm tests — direct against AgentRunActionService.load_messages.

Per RFC-0022 §Reduction 算法, ``load_messages`` IS the canonical fold
implementation (no separate "reference fold" in tests — that earlier
parallel implementation hid a real production UNDO bug for months by
shadowing it). All algebraic / behavioral expectations live here, run
through the real service against a SQLite in-memory engine.

Scenarios covered:

- Pure APPEND (single + multi-message)
- REPLACE (compaction Class B aliasing also tested)
- UNDO over single-action target run
- **UNDO over multi-action target run** (the bug that motivated this file)
- UNDO targeting a non-existent run (silent no-op per current production)
- Multi-page pagination (>200 actions)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from nexau.archs.session.agent_run_action_service import AgentRunActionKey, AgentRunActionService
from nexau.archs.session.models.agent_run_action_model import AgentRunActionModel
from nexau.archs.session.orm.sql_engine import SQLDatabaseEngine
from nexau.core.messages import Message, Role, TextBlock


@asynccontextmanager
async def _service() -> AsyncGenerator[tuple[AgentRunActionService, SQLDatabaseEngine]]:
    eng = SQLDatabaseEngine.from_url("sqlite+aiosqlite:///:memory:")
    try:
        await eng.setup_models([AgentRunActionModel])
        yield AgentRunActionService(engine=eng), eng
    finally:
        await eng._engine.dispose()


def _msg(text: str, role: Role = Role.ASSISTANT) -> Message:
    return Message(role=role, content=[TextBlock(text=text)])


def _texts(msgs: list[Message]) -> list[str]:
    out: list[str] = []
    for m in msgs:
        for b in m.content:
            if isinstance(b, TextBlock):
                out.append(b.text)
    return out


_KW: dict[str, Any] = dict(user_id="u", session_id="s", agent_id="a")


def test_pure_append_single_run():
    async def run():
        async with _service() as (svc, eng):
            await eng.create(
                AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("hello"), _msg("world")], **_KW)
            )
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert _texts(msgs) == ["hello", "world"]

    asyncio.run(run())


def test_pure_append_multi_run():
    async def run():
        async with _service() as (svc, eng):
            for i in range(3):
                await eng.create(AgentRunActionModel.create_append(run_id=f"r{i}", root_run_id=f"r{i}", messages=[_msg(f"r{i}-m")], **_KW))
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert _texts(msgs) == ["r0-m", "r1-m", "r2-m"]

    asyncio.run(run())


def test_replace_supersedes_prior_actions_at_load_layer():
    """REPLACE supersedes earlier history at the LOAD layer only — the
    DESC scan stops at the first REPLACE seen, so prior rows are skipped
    when reconstructing live state. The rows themselves remain on disk
    (event-sourcing append-only); deleting them here would break RFC-0088
    SSOT replay, audit, and billing.
    """

    async def run():
        async with _service() as (svc, eng):
            await eng.create(AgentRunActionModel.create_append(run_id="r0", root_run_id="r0", messages=[_msg("old")], **_KW))
            await svc.persist_replace(
                key=AgentRunActionKey(**_KW),
                run_id="r1",
                root_run_id="r1",
                messages=[_msg("fresh")],
            )

            # Live load returns only "fresh" — REPLACE wins on the DESC scan.
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert _texts(msgs) == ["fresh"]

            # But the prior APPEND row is STILL ON DISK (append-only contract).
            from nexau.archs.session.orm.filters import AndFilter, ComparisonFilter

            rows = await eng.find_many(
                AgentRunActionModel,
                filters=AndFilter(
                    filters=[
                        ComparisonFilter.eq("user_id", _KW["user_id"]),
                        ComparisonFilter.eq("session_id", _KW["session_id"]),
                        ComparisonFilter.eq("agent_id", _KW["agent_id"]),
                    ]
                ),
                order_by=("created_at_ns", "action_id"),
            )
            assert len(rows) == 2, f"REPLACE must NOT delete the prior APPEND row, got rows={[r.action_type for r in rows]}"

    asyncio.run(run())


def test_replace_compaction_class_b_aliasing():
    """Compaction (REPLACE + reason='compact_*') folds same as plain REPLACE."""

    async def run():
        async with _service() as (svc, eng):
            for i in range(3):
                await eng.create(
                    AgentRunActionModel.create_append(run_id=f"r{i}", root_run_id=f"r{i}", messages=[_msg(f"orig-{i}")], **_KW)
                )
            # Compaction
            await eng.create(
                AgentRunActionModel.create_replace(
                    run_id="r_compact",
                    root_run_id="r_compact",
                    messages=[_msg("summary")],
                    reason="compact_auto",
                    **_KW,
                )
            )
            # Continue
            await eng.create(
                AgentRunActionModel.create_append(run_id="r_post", root_run_id="r_post", messages=[_msg("after compact")], **_KW)
            )
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert _texts(msgs) == ["summary", "after compact"]

    asyncio.run(run())


def test_undo_single_action_target_run():
    """UNDO before run X with X having one APPEND."""

    async def run():
        async with _service() as (svc, eng):
            await eng.create(AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("keep")], **_KW))
            await eng.create(AgentRunActionModel.create_append(run_id="r2", root_run_id="r2", messages=[_msg("undo me")], **_KW))
            await eng.create(AgentRunActionModel.create_undo(run_id="r_undo", root_run_id="r_undo", undo_before_run_id="r2", **_KW))
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert _texts(msgs) == ["keep"]

    asyncio.run(run())


def test_undo_multi_action_target_run_bug_repro():
    """UNDO before X where X has RUN_START + APPEND + RUN_END.

    This is the bug case: the old skip_until_run_id logic resets at the FIRST
    matching run_id encountered in DESC scan (which is RUN_END), then leaks
    the rest of run X's actions through. The cutoff_ns fix handles it.
    """

    async def run():
        async with _service() as (svc, eng):
            # r1: RUN_START / APPEND / RUN_END
            await eng.create(AgentRunActionModel.create_run_start(run_id="r1", root_run_id="r1", **_KW))
            await eng.create(AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("r1 keep")], **_KW))
            await eng.create(AgentRunActionModel.create_run_end(run_id="r1", root_run_id="r1", status="ok", **_KW))
            # r2: RUN_START / APPEND / RUN_END (will be undone)
            await eng.create(AgentRunActionModel.create_run_start(run_id="r2", root_run_id="r2", **_KW))
            await eng.create(AgentRunActionModel.create_append(run_id="r2", root_run_id="r2", messages=[_msg("r2 undo me")], **_KW))
            await eng.create(AgentRunActionModel.create_run_end(run_id="r2", root_run_id="r2", status="ok", **_KW))
            # UNDO before r2
            await eng.create(AgentRunActionModel.create_undo(run_id="r_undo", root_run_id="r_undo", undo_before_run_id="r2", **_KW))

            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert _texts(msgs) == ["r1 keep"], (
                f"UNDO leaked through: got {_texts(msgs)}. If 'r2 undo me' appears, the multi-action-per-run UNDO bug is back."
            )

    asyncio.run(run())


def test_undo_multiple_appends_per_target_run():
    """Target run has multiple APPEND rows — all must be removed."""

    async def run():
        async with _service() as (svc, eng):
            await eng.create(AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("r1 keep")], **_KW))
            for i in range(3):
                await eng.create(AgentRunActionModel.create_append(run_id="r2", root_run_id="r2", messages=[_msg(f"r2 iter{i}")], **_KW))
            await eng.create(AgentRunActionModel.create_undo(run_id="r_undo", root_run_id="r_undo", undo_before_run_id="r2", **_KW))

            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert _texts(msgs) == ["r1 keep"]

    asyncio.run(run())


def test_undo_unknown_target_silent_noop():
    """UNDO targeting a non-existent run is a silent no-op (current production behavior).

    NOTE: RFC-0022 §不变量 #2 says fail loud; production silently no-ops.
    Flagging this divergence — to be resolved by either updating production
    to fail loud or relaxing the RFC invariant. For now this test pins
    the actual behavior so it doesn't change accidentally.
    """

    async def run():
        async with _service() as (svc, eng):
            await eng.create(AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("keep")], **_KW))
            await eng.create(
                AgentRunActionModel.create_undo(run_id="r_undo", root_run_id="r_undo", undo_before_run_id="r_does_not_exist", **_KW)
            )

            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert _texts(msgs) == ["keep"]  # UNDO ignored

    asyncio.run(run())


def test_pagination_500_actions():
    """500 APPENDs across multiple pages (page_size=200) all fold correctly."""

    async def run():
        async with _service() as (svc, eng):
            for i in range(500):
                await eng.create(AgentRunActionModel.create_append(run_id=f"r{i}", root_run_id=f"r{i}", messages=[_msg(f"m{i}")], **_KW))
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert len(msgs) == 500
            assert _texts(msgs) == [f"m{i}" for i in range(500)]

    asyncio.run(run())


def test_pagination_with_replace_anchor_short_circuits():
    """REPLACE in middle of a 500-action stream causes early stop (anchor optimization)."""

    async def run():
        async with _service() as (svc, eng):
            # 100 old appends (will be wiped by REPLACE)
            for i in range(100):
                await eng.create(
                    AgentRunActionModel.create_append(run_id=f"old{i}", root_run_id=f"old{i}", messages=[_msg(f"old{i}")], **_KW)
                )
            # REPLACE (anchor)
            await eng.create(
                AgentRunActionModel.create_replace(
                    run_id="r_anchor",
                    root_run_id="r_anchor",
                    messages=[_msg("anchor state")],
                    reason="user_clear",
                    **_KW,
                )
            )
            # 100 new appends post-anchor
            for i in range(100):
                await eng.create(
                    AgentRunActionModel.create_append(run_id=f"new{i}", root_run_id=f"new{i}", messages=[_msg(f"new{i}")], **_KW)
                )

            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            expected = ["anchor state"] + [f"new{i}" for i in range(100)]
            assert _texts(msgs) == expected

    asyncio.run(run())


# ============================================================================
# Empty / boundary / pagination edges
# ============================================================================


def test_empty_session_returns_empty_list():
    async def run():
        async with _service() as (svc, _eng):
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert msgs == []

    asyncio.run(run())


def test_only_run_start_no_append_no_messages():
    async def run():
        async with _service() as (svc, eng):
            await eng.create(AgentRunActionModel.create_run_start(run_id="r1", root_run_id="r1", **_KW))
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert msgs == []

    asyncio.run(run())


def test_only_run_end_no_messages():
    async def run():
        async with _service() as (svc, eng):
            await eng.create(AgentRunActionModel.create_run_end(run_id="r1", root_run_id="r1", status="ok", **_KW))
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert msgs == []

    asyncio.run(run())


def test_run_start_run_end_only_no_messages():
    async def run():
        async with _service() as (svc, eng):
            await eng.create(AgentRunActionModel.create_run_start(run_id="r1", root_run_id="r1", **_KW))
            await eng.create(AgentRunActionModel.create_run_end(run_id="r1", root_run_id="r1", status="ok", **_KW))
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert msgs == []

    asyncio.run(run())


def test_pagination_exact_page_boundary_200():
    """Exactly page_size actions = exactly 1 page + 1 empty page."""

    async def run():
        async with _service() as (svc, eng):
            for i in range(200):
                await eng.create(AgentRunActionModel.create_append(run_id=f"r{i}", root_run_id=f"r{i}", messages=[_msg(f"m{i}")], **_KW))
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert len(msgs) == 200
            assert _texts(msgs) == [f"m{i}" for i in range(200)]

    asyncio.run(run())


def test_pagination_one_over_boundary_201():
    """One past page_size — must roll into next page."""

    async def run():
        async with _service() as (svc, eng):
            for i in range(201):
                await eng.create(AgentRunActionModel.create_append(run_id=f"r{i}", root_run_id=f"r{i}", messages=[_msg(f"m{i}")], **_KW))
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert _texts(msgs) == [f"m{i}" for i in range(201)]

    asyncio.run(run())


# ============================================================================
# REPLACE edges
# ============================================================================


def test_replace_with_empty_messages_clears_state():
    """/clear semantics: REPLACE with empty messages → state == []."""

    async def run():
        async with _service() as (svc, eng):
            await eng.create(AgentRunActionModel.create_append(run_id="r0", root_run_id="r0", messages=[_msg("old1"), _msg("old2")], **_KW))
            await eng.create(
                AgentRunActionModel.create_replace(run_id="r_clear", root_run_id="r_clear", messages=[], reason="user_clear", **_KW)
            )
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert msgs == []

    asyncio.run(run())


def test_multiple_replaces_only_latest_wins():
    """Multiple REPLACEs → only the most recent is the anchor."""

    async def run():
        async with _service() as (svc, eng):
            await eng.create(
                AgentRunActionModel.create_replace(run_id="r1", root_run_id="r1", messages=[_msg("first")], reason="user_clear", **_KW)
            )
            await eng.create(
                AgentRunActionModel.create_replace(run_id="r2", root_run_id="r2", messages=[_msg("second")], reason="user_clear", **_KW)
            )
            await eng.create(
                AgentRunActionModel.create_replace(run_id="r3", root_run_id="r3", messages=[_msg("third")], reason="user_clear", **_KW)
            )
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert _texts(msgs) == ["third"]

    asyncio.run(run())


def test_replace_then_append_appends_after_anchor():
    """REPLACE wipes pre-anchor, APPENDs after add to it."""

    async def run():
        async with _service() as (svc, eng):
            await eng.create(AgentRunActionModel.create_append(run_id="r0", root_run_id="r0", messages=[_msg("wiped")], **_KW))
            await eng.create(
                AgentRunActionModel.create_replace(run_id="r1", root_run_id="r1", messages=[_msg("anchor")], reason="user_clear", **_KW)
            )
            await eng.create(AgentRunActionModel.create_append(run_id="r2", root_run_id="r2", messages=[_msg("after")], **_KW))
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert _texts(msgs) == ["anchor", "after"]

    asyncio.run(run())


def test_compaction_three_reasons_all_anchor_correctly():
    """compact_auto / compact_manual / compact_focused all act as REPLACE anchors.

    Each variant uses its typed factory to enforce required fields at write time
    (e.g. compact_focused requires focus_instructions).
    """

    async def run():
        async def _check(label: str, build_replace):
            async with _service() as (svc, eng):
                await eng.create(AgentRunActionModel.create_append(run_id="r0", root_run_id="r0", messages=[_msg("wipe me")], **_KW))
                await eng.create(build_replace(eng))
                msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
                assert _texts(msgs) == [f"summary [{label}]"], f"{label!r} did not anchor REPLACE: {_texts(msgs)}"

        await _check(
            "compact_auto",
            lambda eng: AgentRunActionModel.create_replace_compact_auto(
                run_id="r1", root_run_id="r1", messages=[_msg("summary [compact_auto]")], **_KW
            ),
        )
        await _check(
            "compact_manual",
            lambda eng: AgentRunActionModel.create_replace_compact_manual(
                run_id="r1", root_run_id="r1", messages=[_msg("summary [compact_manual]")], **_KW
            ),
        )
        await _check(
            "compact_focused",
            lambda eng: AgentRunActionModel.create_replace_compact_focused(
                run_id="r1",
                root_run_id="r1",
                messages=[_msg("summary [compact_focused]")],
                focus_instructions="focus on X",
                **_KW,
            ),
        )

    asyncio.run(run())


# ============================================================================
# APPEND edges (dedup, system filtering)
# ============================================================================


def test_same_message_id_in_two_appends_latest_wins():
    """Re-applied message (same .id) deduplicated; latest version wins.

    Mirrors the production ``apply_messages`` ``message_by_id`` dedup logic —
    Phase 2 iter-level streaming may rewrite the same Message id mid-run.
    """

    async def run():
        async with _service() as (svc, eng):
            from uuid import uuid4

            shared_id = uuid4()
            m_first = Message(id=shared_id, role=Role.ASSISTANT, content=[TextBlock(text="initial")])
            m_updated = Message(id=shared_id, role=Role.ASSISTANT, content=[TextBlock(text="updated")])
            other = _msg("other")

            await eng.create(AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[m_first, other], **_KW))
            await eng.create(AgentRunActionModel.create_append(run_id="r2", root_run_id="r2", messages=[m_updated], **_KW))

            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            # Position preserved (first slot), text updated, no duplication
            assert len(msgs) == 2
            assert _texts(msgs) == ["updated", "other"]

    asyncio.run(run())


def test_system_messages_filtered_from_load():
    """SYSTEM-role messages are dropped from load output."""

    async def run():
        async with _service() as (svc, eng):
            user_m = _msg("user msg", role=Role.USER)
            asst_m = _msg("asst msg", role=Role.ASSISTANT)
            # persist_append filters system at write time, so use direct create with raw factory
            # (factory accepts system but production load filters them out)
            sys_m = _msg("sys msg", role=Role.SYSTEM)
            await eng.create(AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[user_m, sys_m, asst_m], **_KW))

            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            roles = [m.role for m in msgs]
            assert Role.SYSTEM not in roles
            assert _texts(msgs) == ["user msg", "asst msg"]

    asyncio.run(run())


# ============================================================================
# UNDO edges
# ============================================================================


def test_undo_nested_takes_earliest_cutoff():
    """Two UNDOs nest by the earliest target's cutoff_ns winning."""

    async def run():
        async with _service() as (svc, eng):
            await eng.create(AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("r1 keep")], **_KW))
            await eng.create(AgentRunActionModel.create_append(run_id="r2", root_run_id="r2", messages=[_msg("r2 undone")], **_KW))
            await eng.create(
                AgentRunActionModel.create_append(run_id="r3", root_run_id="r3", messages=[_msg("r3 also undone (by inner)")], **_KW)
            )
            # Inner UNDO targets r3 (newer)
            await eng.create(
                AgentRunActionModel.create_undo(run_id="r_undo_inner", root_run_id="r_undo_inner", undo_before_run_id="r3", **_KW)
            )
            # Outer UNDO targets r2 (older). Should win cutoff (earliest).
            await eng.create(
                AgentRunActionModel.create_undo(run_id="r_undo_outer", root_run_id="r_undo_outer", undo_before_run_id="r2", **_KW)
            )

            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert _texts(msgs) == ["r1 keep"]

    asyncio.run(run())


def test_undo_then_replace_replace_wipes_undo():
    """REPLACE after UNDO wipes everything including the UNDO's cutoff."""

    async def run():
        async with _service() as (svc, eng):
            await eng.create(AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("old")], **_KW))
            await eng.create(AgentRunActionModel.create_undo(run_id="r_undo", root_run_id="r_undo", undo_before_run_id="r1", **_KW))
            await eng.create(
                AgentRunActionModel.create_replace(
                    run_id="r_replace", root_run_id="r_replace", messages=[_msg("fresh start")], reason="user_clear", **_KW
                )
            )
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert _texts(msgs) == ["fresh start"]

    asyncio.run(run())


def test_undo_targeting_first_run_wipes_everything():
    """UNDO before X where X is the very first run → state == []."""

    async def run():
        async with _service() as (svc, eng):
            await eng.create(AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("a")], **_KW))
            await eng.create(AgentRunActionModel.create_append(run_id="r2", root_run_id="r2", messages=[_msg("b")], **_KW))
            await eng.create(AgentRunActionModel.create_undo(run_id="r_undo", root_run_id="r_undo", undo_before_run_id="r1", **_KW))
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert msgs == []

    asyncio.run(run())


def test_undo_target_only_has_run_start_no_append():
    """UNDO before X where X has only RUN_START (no APPEND) → RUN_START's ns anchors cutoff."""

    async def run():
        async with _service() as (svc, eng):
            await eng.create(AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("keep")], **_KW))
            await eng.create(AgentRunActionModel.create_run_start(run_id="r2", root_run_id="r2", **_KW))
            # No APPEND for r2 — r2 only exists as RUN_START
            await eng.create(AgentRunActionModel.create_run_end(run_id="r2", root_run_id="r2", status="ok", **_KW))
            await eng.create(AgentRunActionModel.create_undo(run_id="r_undo", root_run_id="r_undo", undo_before_run_id="r2", **_KW))
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            # RUN_START contributed no messages, but it's still findable as r2's first action.
            # Cutoff = r2 RUN_START's ns. r1 keeps; r2 markers (no messages anyway) wiped.
            assert _texts(msgs) == ["keep"]

    asyncio.run(run())


def test_two_independent_undos_different_targets():
    """Two UNDOs in different runs target different earlier runs → both cutoffs apply (min wins)."""

    async def run():
        async with _service() as (svc, eng):
            for i in range(5):
                await eng.create(AgentRunActionModel.create_append(run_id=f"r{i}", root_run_id=f"r{i}", messages=[_msg(f"m{i}")], **_KW))
            # First UNDO targets r3 (cutoff = r3's ns)
            await eng.create(AgentRunActionModel.create_undo(run_id="r_u1", root_run_id="r_u1", undo_before_run_id="r3", **_KW))
            # Add some content (which itself will be cut by the second UNDO)
            await eng.create(
                AgentRunActionModel.create_append(run_id="r_after_u1", root_run_id="r_after_u1", messages=[_msg("after u1")], **_KW)
            )
            # Second UNDO targets r2 (cutoff = r2's ns < r3's ns → wins)
            await eng.create(AgentRunActionModel.create_undo(run_id="r_u2", root_run_id="r_u2", undo_before_run_id="r2", **_KW))

            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            # Cutoff is r2's ns → keep m0, m1; drop m2..m4 + after_u1
            assert _texts(msgs) == ["m0", "m1"]

    asyncio.run(run())


def test_undo_in_same_run_as_following_append_does_not_undo_that_append():
    """An UNDO row's run_id is independent; an APPEND with the same run_id but later
    timestamp is OUTSIDE the cutoff (cutoff is target's first ns, not UNDO row's ns)."""

    async def run():
        async with _service() as (svc, eng):
            await eng.create(AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("undone")], **_KW))
            # UNDO and then APPEND share run_id="r_after"; both happen after UNDO's ns
            await eng.create(AgentRunActionModel.create_undo(run_id="r_after", root_run_id="r_after", undo_before_run_id="r1", **_KW))
            await eng.create(
                AgentRunActionModel.create_append(run_id="r_after", root_run_id="r_after", messages=[_msg("kept after undo")], **_KW)
            )
            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            # cutoff = r1's ns. UNDO row and the post-UNDO APPEND both have ns > r1's ns,
            # but the post-UNDO APPEND is NOT in r1, so it's NOT undone.
            assert _texts(msgs) == ["kept after undo"]

    asyncio.run(run())


# ============================================================================
# Cross-isolation (negative cases) — wrong key returns nothing / no leakage
# ============================================================================


def test_isolation_different_user_id():
    async def run():
        async with _service() as (svc, eng):
            await eng.create(
                AgentRunActionModel.create_append(
                    user_id="alice", session_id="s", agent_id="a", run_id="r1", root_run_id="r1", messages=[_msg("alice msg")]
                )
            )
            # Bob's load is empty
            bob_msgs = await svc.load_messages(key=AgentRunActionKey(user_id="bob", session_id="s", agent_id="a"))
            assert bob_msgs == []

    asyncio.run(run())


def test_isolation_different_session_id():
    async def run():
        async with _service() as (svc, eng):
            await eng.create(
                AgentRunActionModel.create_append(
                    user_id="u", session_id="s1", agent_id="a", run_id="r1", root_run_id="r1", messages=[_msg("s1 msg")]
                )
            )
            other = await svc.load_messages(key=AgentRunActionKey(user_id="u", session_id="s2", agent_id="a"))
            assert other == []

    asyncio.run(run())


def test_isolation_different_agent_id():
    """Same session, different agents → independent fold scopes."""

    async def run():
        async with _service() as (svc, eng):
            await eng.create(
                AgentRunActionModel.create_append(
                    user_id="u", session_id="s", agent_id="parent", run_id="rp", root_run_id="rp", messages=[_msg("parent")]
                )
            )
            await eng.create(
                AgentRunActionModel.create_append(
                    user_id="u",
                    session_id="s",
                    agent_id="child",
                    run_id="rc",
                    root_run_id="rp",
                    parent_run_id="rp",
                    messages=[_msg("child")],
                )
            )

            parent_msgs = await svc.load_messages(key=AgentRunActionKey(user_id="u", session_id="s", agent_id="parent"))
            child_msgs = await svc.load_messages(key=AgentRunActionKey(user_id="u", session_id="s", agent_id="child"))
            assert _texts(parent_msgs) == ["parent"]
            assert _texts(child_msgs) == ["child"]

    asyncio.run(run())


def test_legacy_non_uuid_message_id_round_trips():
    """Legacy producers (external imports) wrote ``msg.id`` as non-UUID strings
    like ``"msg-run-1775987721028-23698-1-history-1"``. Production must load,
    fold, and round-trip these intact (no UUID validation crash, no silent
    coercion that changes the id literal).
    """

    async def run():
        async with _service() as (svc, eng):
            legacy_id_1 = "msg-run-1775987721028-23698-1-history-1"
            legacy_id_2 = "msg-run-1775987721028-23698-1-history-2"
            m1 = Message(id=legacy_id_1, role=Role.USER, content=[TextBlock(text="legacy 1")])
            m2 = Message(id=legacy_id_2, role=Role.ASSISTANT, content=[TextBlock(text="legacy 2")])

            await eng.create(AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[m1, m2], **_KW))

            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            # Ids preserved verbatim — no UUID5 hashing or other coercion
            assert [m.id for m in msgs] == [legacy_id_1, legacy_id_2]
            assert _texts(msgs) == ["legacy 1", "legacy 2"]

    asyncio.run(run())


def test_legacy_non_uuid_message_id_dedup_stable():
    """Same legacy id appearing in two APPENDs deduplicates correctly."""

    async def run():
        async with _service() as (svc, eng):
            legacy_id = "msg-run-1775987721028-23698-1-history-7"
            m_first = Message(id=legacy_id, role=Role.ASSISTANT, content=[TextBlock(text="first")])
            m_updated = Message(id=legacy_id, role=Role.ASSISTANT, content=[TextBlock(text="rewritten")])
            other = _msg("other")

            await eng.create(AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[m_first, other], **_KW))
            await eng.create(AgentRunActionModel.create_append(run_id="r2", root_run_id="r2", messages=[m_updated], **_KW))

            msgs = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert len(msgs) == 2
            assert _texts(msgs) == ["rewritten", "other"]
            # Ids remain in legacy format
            assert msgs[0].id == legacy_id

    asyncio.run(run())


def test_message_id_accepts_uuid_object_and_coerces_to_str():
    """Backward-compat: callers passing ``UUID()`` (old API) get auto-coerced to str."""
    from uuid import uuid4

    u = uuid4()
    m = Message(id=u, role=Role.ASSISTANT, content=[TextBlock(text="x")])
    assert m.id == str(u)
    assert isinstance(m.id, str)


def test_message_id_default_is_uuid4_string():
    """Default-generated id is still a UUID4-formatted string (regression guard)."""
    from uuid import UUID as _UUID

    m = Message(role=Role.ASSISTANT, content=[TextBlock(text="x")])
    assert isinstance(m.id, str)
    # If it parses as UUID v4, default factory still produces canonical ids
    parsed = _UUID(m.id)
    assert parsed.version == 4


def test_isolation_undo_does_not_cross_agent_boundary():
    """UNDO in agent A's stream must not affect agent B's actions even if same run_id existed."""

    async def run():
        async with _service() as (svc, eng):
            # Agent A
            await eng.create(
                AgentRunActionModel.create_append(
                    user_id="u", session_id="s", agent_id="A", run_id="r1", root_run_id="r1", messages=[_msg("A's r1")]
                )
            )
            await eng.create(
                AgentRunActionModel.create_undo(
                    user_id="u", session_id="s", agent_id="A", run_id="r_u", root_run_id="r_u", undo_before_run_id="r1"
                )
            )
            # Agent B has an action with run_id="r1" too (independent)
            await eng.create(
                AgentRunActionModel.create_append(
                    user_id="u", session_id="s", agent_id="B", run_id="r1", root_run_id="r1", messages=[_msg("B's r1 untouched")]
                )
            )

            a_msgs = await svc.load_messages(key=AgentRunActionKey(user_id="u", session_id="s", agent_id="A"))
            b_msgs = await svc.load_messages(key=AgentRunActionKey(user_id="u", session_id="s", agent_id="B"))
            assert a_msgs == []  # A's r1 was undone
            assert _texts(b_msgs) == ["B's r1 untouched"]  # B's r1 untouched

    asyncio.run(run())


# ============================================================================
# Stream-interrupted artifacts — write-side + read-side defenses
# ============================================================================


def test_persist_drops_reasoning_only_assistant_message():
    """LLM stream cut after reasoning chunks but before text/tool_use: the
    incomplete message has no semantic value and breaks DeepSeek-style
    providers on resume. ``persist_append`` filters them at write time.
    """
    from nexau.core.messages import ReasoningBlock

    async def run():
        async with _service() as (svc, _eng):
            user_msg = _msg("hello", role=Role.USER)
            reasoning_only = Message(
                role=Role.ASSISTANT,
                content=[ReasoningBlock(text="thinking but stream got cut...")],
            )
            real_assistant = Message(
                role=Role.ASSISTANT,
                content=[ReasoningBlock(text="proper turn"), TextBlock(text="ok")],
            )
            # persist_append filters SYSTEM + reasoning-only; keeps the rest.
            await svc.persist_append(
                key=AgentRunActionKey(**_KW),
                run_id="r1",
                root_run_id="r1",
                parent_run_id=None,
                agent_name="a",
                messages=[user_msg, reasoning_only, real_assistant],
            )
            loaded = await svc.load_messages(key=AgentRunActionKey(**_KW))
            # Reasoning-only assistant filtered; user + real assistant remain
            assert _texts(loaded) == ["hello", "ok"]
            roles = [m.role for m in loaded]
            assert roles == [Role.USER, Role.ASSISTANT]

    asyncio.run(run())


def test_persist_rejects_when_only_message_is_reasoning_only():
    """If the entire batch is reasoning-only, persist_append raises (no APPEND
    row written rather than an empty extra)."""
    import pytest as _pytest

    from nexau.core.messages import ReasoningBlock

    async def run():
        async with _service() as (svc, _eng):
            reasoning_only = Message(
                role=Role.ASSISTANT,
                content=[ReasoningBlock(text="only thinking")],
            )
            with _pytest.raises(ValueError, match="no messages"):
                await svc.persist_append(
                    key=AgentRunActionKey(**_KW),
                    run_id="r1",
                    root_run_id="r1",
                    parent_run_id=None,
                    agent_name="a",
                    messages=[reasoning_only],
                )

    asyncio.run(run())


def test_load_synthesizes_tool_result_for_orphan_tool_use():
    """Tool execution crashed before tool_result was persisted — load_messages
    injects a synthetic tool_result so the messages list still satisfies
    Anthropic API's tool_use ↔ tool_result pairing requirement on resume.
    """
    from nexau.core.messages import ToolResultBlock, ToolUseBlock

    async def run():
        async with _service() as (svc, eng):
            user_msg = _msg("call X", role=Role.USER)
            assistant_with_orphan_tool_use = Message(
                role=Role.ASSISTANT,
                content=[
                    TextBlock(text="I'll do X."),
                    ToolUseBlock(id="tool_call_1", name="do_x", input={"arg": 1}),
                ],
            )
            # NOTE: NO matching tool_result message follows — orphan
            await eng.create(
                AgentRunActionModel.create_append(
                    run_id="r1",
                    root_run_id="r1",
                    messages=[user_msg, assistant_with_orphan_tool_use],
                    **_KW,
                )
            )
            loaded = await svc.load_messages(key=AgentRunActionKey(**_KW))
            # Assistant with orphan kept + synthetic tool_result inserted right after
            assert len(loaded) == 3
            assert loaded[0].role == Role.USER
            assert loaded[1].role == Role.ASSISTANT
            assert loaded[2].role == Role.TOOL
            # Synthetic block is a ToolResultBlock with is_error=True and matching id
            tr_blocks = [b for b in loaded[2].content if isinstance(b, ToolResultBlock)]
            assert len(tr_blocks) == 1
            assert tr_blocks[0].tool_use_id == "tool_call_1"
            assert tr_blocks[0].is_error is True
            assert "did not complete" in (tr_blocks[0].content if isinstance(tr_blocks[0].content, str) else "")

    asyncio.run(run())


def test_load_does_not_synthesize_when_tool_result_already_paired():
    """If tool_result already exists, no synthesis happens (idempotent)."""
    from nexau.core.messages import ToolResultBlock, ToolUseBlock

    async def run():
        async with _service() as (svc, eng):
            assistant = Message(
                role=Role.ASSISTANT,
                content=[ToolUseBlock(id="tu_1", name="x", input={})],
            )
            tool_result = Message(
                role=Role.TOOL,
                content=[ToolResultBlock(tool_use_id="tu_1", content="ok", is_error=False)],
            )
            await eng.create(AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[assistant, tool_result], **_KW))
            loaded = await svc.load_messages(key=AgentRunActionKey(**_KW))
            assert len(loaded) == 2  # no synthesis
            tr_blocks = [b for b in loaded[1].content if isinstance(b, ToolResultBlock)]
            assert len(tr_blocks) == 1
            assert tr_blocks[0].is_error is False
            assert tr_blocks[0].content == "ok"  # original preserved

    asyncio.run(run())


def test_load_synthesizes_only_for_unpaired_when_some_pairs_exist():
    """Mixed scenario: 2 tool_use, only 1 has tool_result. Synthesize for the orphan only."""
    from nexau.core.messages import ToolResultBlock, ToolUseBlock

    async def run():
        async with _service() as (svc, eng):
            assistant = Message(
                role=Role.ASSISTANT,
                content=[
                    ToolUseBlock(id="tu_paired", name="x", input={}),
                    ToolUseBlock(id="tu_orphan", name="y", input={}),
                ],
            )
            tool_result_paired = Message(
                role=Role.TOOL,
                content=[ToolResultBlock(tool_use_id="tu_paired", content="ok", is_error=False)],
            )
            await eng.create(
                AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[assistant, tool_result_paired], **_KW)
            )
            loaded = await svc.load_messages(key=AgentRunActionKey(**_KW))
            # Should have: assistant, original tool_result(tu_paired), synthetic(tu_orphan)
            assert len(loaded) == 3
            tr_ids = []
            tr_is_errors = []
            for m in loaded:
                if m.role == Role.TOOL:
                    for b in m.content:
                        if isinstance(b, ToolResultBlock):
                            tr_ids.append(b.tool_use_id)
                            tr_is_errors.append(b.is_error)
            assert set(tr_ids) == {"tu_paired", "tu_orphan"}
            paired_is_error = tr_is_errors[tr_ids.index("tu_paired")]
            orphan_is_error = tr_is_errors[tr_ids.index("tu_orphan")]
            assert paired_is_error is False
            assert orphan_is_error is True

    asyncio.run(run())


def test_load_drops_reasoning_only_legacy_message():
    """Read-side defense: legacy reasoning-only messages already in DB are
    filtered out at load time so they don't reach DeepSeek-style providers."""
    from nexau.core.messages import ReasoningBlock

    async def run():
        async with _service() as (svc, eng):
            # Inject a reasoning-only assistant message via raw model construction
            # (bypasses persist_append filter to simulate legacy data).
            user_m = _msg("hi", role=Role.USER)
            reasoning_only = Message(role=Role.ASSISTANT, content=[ReasoningBlock(text="legacy stuck thought")])
            real_assistant = Message(role=Role.ASSISTANT, content=[TextBlock(text="proper response")])
            # Direct engine.create — bypasses service-layer filter (mimics legacy data).
            await eng.create(
                AgentRunActionModel.create_append(
                    run_id="r1",
                    root_run_id="r1",
                    messages=[user_m, reasoning_only, real_assistant],
                    **_KW,
                )
            )
            loaded = await svc.load_messages(key=AgentRunActionKey(**_KW))
            # Reasoning-only filtered at read; user + real assistant remain
            assert _texts(loaded) == ["hi", "proper response"]
            assert all(
                not (m.role == Role.ASSISTANT and m.content and all(isinstance(b, ReasoningBlock) for b in m.content)) for m in loaded
            )

    asyncio.run(run())
