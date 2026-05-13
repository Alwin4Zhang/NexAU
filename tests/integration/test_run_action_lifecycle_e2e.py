# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""End-to-end Cookbook scenario tests for AgentRunActionModel (RFC-0022).

Phase 1 protocol changes (PR #503) added RunActionType enum members + 3
columns + typed *Extra discriminated union, but NO production consumer
actually uses the new types yet (Phase 2 wires AgentRunner; Phase 3
wires ContextCompactionMiddleware; Phase 4 wires SubAgentManager).

Without a real consumer, the only way to validate the protocol is to
**simulate Phase 2/3 consumer behavior** in tests:

- Construct the mutation sequences described in RFC-0022 §Action Cookbook
  scenarios 1-9
- Persist them through ``AgentRunActionService`` (existing API for
  APPEND/UNDO/REPLACE) + direct ``engine.create()`` (for new lifecycle
  types RUN_START/RUN_END that don't yet have service helpers)
- Verify the canonical fold algorithm (from ``test_run_action_algebra``)
  reconstructs the expected messages state

What this catches:
- New typed *Extra serialization through the real DB layer (already
  covered by test_run_action_db_roundtrip, but combined with multi-row
  scenarios here)
- Idempotency_key UNIQUE behavior under realistic Phase 2-style writes
- Per-agent fold scope (agent_id filter) — protects the multi-agent case
- Phase 1 backward compatibility: existing ``load_messages()`` consumer
  works for APPEND/UNDO/REPLACE (compaction is Class B aliased onto
  REPLACE — RFC-0022 §设计原则 §6 — so it's also handled correctly
  without any consumer-side changes)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.exc import IntegrityError

from nexau.archs.session.agent_run_action_service import AgentRunActionKey, AgentRunActionService
from nexau.archs.session.models.agent_run_action_model import (
    AgentRunActionModel,
    AppendExtra,
    CompactAutoVariant,
    CompactFocusedVariant,
    CompactStats,
    RunActionType,
    RunEndExtra,
    RunStartExtra,
    UserClearVariant,
)
from nexau.archs.session.orm.filters import AndFilter, ComparisonFilter
from nexau.archs.session.orm.sql_engine import SQLDatabaseEngine
from nexau.core.messages import Message, Role, TextBlock

# ============================================================================
# Test harness — real SQLite engine + service
# ============================================================================


@asynccontextmanager
async def _service() -> AsyncGenerator[tuple[AgentRunActionService, SQLDatabaseEngine]]:
    """Real SQLite engine + AgentRunActionService for e2e."""
    eng = SQLDatabaseEngine.from_url("sqlite+aiosqlite:///:memory:")
    try:
        await eng.setup_models([AgentRunActionModel])
        yield AgentRunActionService(engine=eng), eng
    finally:
        await eng._engine.dispose()


def _msg(text: str, role: Role = Role.ASSISTANT) -> Message:
    return Message(role=role, content=[TextBlock(text=text)])


def _texts(msgs: list[Message]) -> list[tuple[str, str]]:
    """Reduce messages to (role, text) tuples for equality-by-content comparison.

    Pydantic Message equality includes ``created_at`` (set by persistence
    layer at write time), so direct ``msg in loaded`` fails. Compare by
    semantic content instead.
    """
    return [(m.role.value, "".join(b.text for b in m.content if isinstance(b, TextBlock))) for m in msgs]


async def _all_actions(eng: SQLDatabaseEngine, *, session_id: str, agent_id: str) -> list[AgentRunActionModel]:
    """Read all actions in canonical fold order."""
    return await eng.find_many(
        AgentRunActionModel,
        filters=AndFilter(
            filters=[
                ComparisonFilter.eq("session_id", session_id),
                ComparisonFilter.eq("agent_id", agent_id),
            ]
        ),
        order_by=("created_at_ns", "action_id"),
    )


# NOTE: There used to be a ``_canonical_fold`` reference implementation here.
# It was deleted because it shadowed production semantics — every Cookbook
# scenario was being verified against the in-memory reference rather than
# the actual ``AgentRunActionService.load_messages``, which let a real UNDO
# bug ship undetected (see ``test_load_messages_semantics`` for the bug
# reproducer). All assertions now go through ``svc.load_messages`` directly.


# ============================================================================
# Cookbook 场景 1: 基础 run flow (RUN_START → APPEND × N → RUN_END)
# ============================================================================


def test_scenario_1_basic_run_lifecycle():
    """RFC §Cookbook 场景 1: 用户首次发起 run, 多 iter APPEND, RUN_END 收尾.

    Models the full Phase 2 iter-level flow and verifies the canonical
    fold reconstructs the expected messages state.
    """

    async def run():
        async with _service() as (svc, eng):
            key = AgentRunActionKey(user_id="u", session_id="s1", agent_id="agent_main")
            run_id = "r1"

            # RUN_START — Phase 2 AgentRunner emits this at run start
            rs = AgentRunActionModel.create_run_start(
                user_id=key.user_id,
                session_id=key.session_id,
                agent_id=key.agent_id,
                run_id=run_id,
                root_run_id=run_id,
                trace_id="trace-r1",
                idempotency_key=f"{run_id}:start",
            )
            await eng.create(rs)

            # APPEND iter 1 — user message + assistant tool round
            user_msg = _msg("Hello agent", role=Role.USER)
            asst_iter1 = _msg("I'll help. Let me check the weather.")
            iter1 = AgentRunActionModel.create_append(
                user_id=key.user_id,
                session_id=key.session_id,
                agent_id=key.agent_id,
                run_id=run_id,
                root_run_id=run_id,
                messages=[user_msg, asst_iter1],
                iter_index=1,
                idempotency_key=f"{run_id}:1",
            )
            await eng.create(iter1)

            # APPEND iter 2 — final response
            asst_final = _msg("It's sunny in Tokyo.")
            iter2 = AgentRunActionModel.create_append(
                user_id=key.user_id,
                session_id=key.session_id,
                agent_id=key.agent_id,
                run_id=run_id,
                root_run_id=run_id,
                messages=[asst_final],
                iter_index=2,
                idempotency_key=f"{run_id}:2",
            )
            await eng.create(iter2)

            # RUN_END — Phase 2 AgentRunner emits at run completion
            re = AgentRunActionModel.create_run_end(
                user_id=key.user_id,
                session_id=key.session_id,
                agent_id=key.agent_id,
                run_id=run_id,
                root_run_id=run_id,
                status="ok",
                idempotency_key=f"{run_id}:end",
            )
            await eng.create(re)

            # Verify fold
            actions = await _all_actions(eng, session_id=key.session_id, agent_id=key.agent_id)
            assert len(actions) == 4
            assert [a.action_type for a in actions] == [
                RunActionType.RUN_START,
                RunActionType.APPEND,
                RunActionType.APPEND,
                RunActionType.RUN_END,
            ]
            state = await svc.load_messages(key=key)
            assert _texts(state) == _texts([user_msg, asst_iter1, asst_final])

            # Verify typed extras roundtrip
            rs_extra = actions[0].parse_extra()
            assert isinstance(rs_extra, RunStartExtra)
            assert rs_extra.trace_id == "trace-r1"

            iter1_extra = actions[1].parse_extra()
            assert isinstance(iter1_extra, AppendExtra)
            assert iter1_extra.iter_index == 1

            re_extra = actions[3].parse_extra()
            assert isinstance(re_extra, RunEndExtra)
            assert re_extra.status == "ok"

    asyncio.run(run())


# ============================================================================
# Cookbook 场景 2: /clear (REPLACE + ReplaceExtra reason="user_clear")
# ============================================================================


def test_scenario_2_user_clear():
    """RFC §Cookbook 场景 2: /clear command — REPLACE payload=[], reason='user_clear'.

    Uses direct engine.create rather than persist_replace because this test
    exercises the model layer directly (variant dispatch). persist_replace
    is now append-only as of the GC removal — see test_run_action_typed_replace
    for the service-level coverage.
    """

    async def run():
        async with _service() as (svc, eng):
            key = AgentRunActionKey(user_id="u", session_id="s2", agent_id="a")

            # Some prior content
            await eng.create(
                AgentRunActionModel.create_append(
                    user_id=key.user_id,
                    session_id=key.session_id,
                    agent_id=key.agent_id,
                    run_id="r1",
                    root_run_id="r1",
                    messages=[_msg("old content")],
                )
            )

            # /clear → REPLACE empty with reason
            await eng.create(
                AgentRunActionModel.create_replace(
                    user_id=key.user_id,
                    session_id=key.session_id,
                    agent_id=key.agent_id,
                    run_id="r_clear",
                    root_run_id="r_clear",
                    messages=[],
                    reason="user_clear",
                )
            )

            actions = await _all_actions(eng, session_id=key.session_id, agent_id=key.agent_id)
            state = await svc.load_messages(key=key)
            assert state == []  # REPLACE wipes everything

            # Verify variant dispatch: reason="user_clear" → UserClearVariant
            replace = actions[1]
            assert replace.action_type == RunActionType.REPLACE
            extra = replace.parse_extra()
            assert isinstance(extra, UserClearVariant)
            assert extra.reason == "user_clear"

    asyncio.run(run())


# ============================================================================
# Cookbook 场景 3 + 4: /compact (auto / manual_focused)
# ============================================================================


def test_scenario_3_auto_compact():
    """RFC §Cookbook 场景 3: auto sliding-window compaction mid-run.

    Class B aliasing (RFC-0022 §设计原则 §6): compaction = REPLACE +
    ReplaceExtra(reason='compact_auto', strategy=..., stats=...) instead of
    a separate ``COMPACT`` action_type. Old SDK readers fold it as a plain
    REPLACE, getting semantically-correct state (no silent context overflow).
    """

    async def run():
        async with _service() as (svc, eng):
            key = AgentRunActionKey(user_id="u", session_id="s3", agent_id="a")

            # Original content (3 messages)
            for i in range(3):
                await eng.create(
                    AgentRunActionModel.create_append(
                        user_id=key.user_id,
                        session_id=key.session_id,
                        agent_id=key.agent_id,
                        run_id=f"r{i}",
                        root_run_id=f"r{i}",
                        messages=[_msg(f"orig {i}")],
                    )
                )

            # Auto compaction: summary message + last 1 retained
            summary = _msg("Summary: discussed orig 0/1/2", role=Role.USER)
            kept = _msg("orig 2")
            await eng.create(
                AgentRunActionModel.create_replace_compact_auto(
                    user_id=key.user_id,
                    session_id=key.session_id,
                    agent_id=key.agent_id,
                    run_id="r_compact",
                    root_run_id="r_compact",
                    messages=[summary, kept],
                    strategy="sliding_window",
                    stats=CompactStats(pre_message_count=3, post_message_count=2),
                )
            )

            # Continue after compact
            new = _msg("new question")
            await eng.create(
                AgentRunActionModel.create_append(
                    user_id=key.user_id,
                    session_id=key.session_id,
                    agent_id=key.agent_id,
                    run_id="r_post",
                    root_run_id="r_post",
                    messages=[new],
                )
            )

            actions = await _all_actions(eng, session_id=key.session_id, agent_id=key.agent_id)
            state = await svc.load_messages(key=key)
            # Compaction wiped origs (REPLACE semantics), kept summary+kept,
            # then appended new
            assert _texts(state) == _texts([summary, kept, new])

            # Verify variant dispatch: CompactAutoVariant — strategy / stats accessible
            compact = next(a for a in actions if a.action_type == RunActionType.REPLACE and a.run_id == "r_compact")
            extra = compact.parse_extra()
            assert isinstance(extra, CompactAutoVariant)
            assert extra.reason == "compact_auto"
            assert extra.strategy == "sliding_window"
            assert extra.stats is not None
            assert extra.stats.pre_message_count == 3
            assert extra.stats.post_message_count == 2

    asyncio.run(run())


def test_scenario_4_manual_focused_compact():
    """RFC §Cookbook 场景 4: /compact [instructions] — user focuses what to keep.

    Same Class B aliasing as scenario 3: REPLACE + reason='compact_focused'.
    """

    async def run():
        async with _service() as (svc, eng):
            key = AgentRunActionKey(user_id="u", session_id="s4", agent_id="a")

            await eng.create(
                AgentRunActionModel.create_replace_compact_focused(
                    user_id=key.user_id,
                    session_id=key.session_id,
                    agent_id=key.agent_id,
                    run_id="r1",
                    root_run_id="r1",
                    messages=[_msg("Focused summary about RFC-0022")],
                    focus_instructions="保留所有关于 RFC-0022 的讨论",
                    strategy="sliding_window",
                )
            )

            actions = await _all_actions(eng, session_id=key.session_id, agent_id=key.agent_id)
            extra = actions[0].parse_extra()
            assert isinstance(extra, CompactFocusedVariant)
            assert extra.reason == "compact_focused"
            assert extra.focus_instructions == "保留所有关于 RFC-0022 的讨论"
            assert extra.strategy == "sliding_window"
            # And the row reads back as REPLACE so old SDK readers fold it
            # via the existing REPLACE branch (no silent context overflow).
            assert actions[0].action_type == RunActionType.REPLACE

    asyncio.run(run())


# ============================================================================
# Cookbook 场景 5/6: /rewind + 用户编辑重发 (UNDO)
# ============================================================================


def test_scenario_5_rewind():
    """RFC §Cookbook 场景 5: /rewind 回到某 run 之前.

    UNDO restores state to snapshots[undo_before_run_id], which was taken
    at that run's RUN_START.
    """

    async def run():
        async with _service() as (svc, eng):
            key = AgentRunActionKey(user_id="u", session_id="s5", agent_id="a")

            # Run r1
            msg_r1 = _msg("r1 content")
            await eng.create(
                AgentRunActionModel.create_run_start(
                    user_id=key.user_id,
                    session_id=key.session_id,
                    agent_id=key.agent_id,
                    run_id="r1",
                    root_run_id="r1",
                )
            )
            await eng.create(
                AgentRunActionModel.create_append(
                    user_id=key.user_id,
                    session_id=key.session_id,
                    agent_id=key.agent_id,
                    run_id="r1",
                    root_run_id="r1",
                    messages=[msg_r1],
                )
            )
            await eng.create(
                AgentRunActionModel.create_run_end(
                    user_id=key.user_id,
                    session_id=key.session_id,
                    agent_id=key.agent_id,
                    run_id="r1",
                    root_run_id="r1",
                    status="ok",
                )
            )

            # Run r2
            msg_r2 = _msg("r2 content (will be undone)")
            await eng.create(
                AgentRunActionModel.create_run_start(
                    user_id=key.user_id,
                    session_id=key.session_id,
                    agent_id=key.agent_id,
                    run_id="r2",
                    root_run_id="r2",
                )
            )
            await eng.create(
                AgentRunActionModel.create_append(
                    user_id=key.user_id,
                    session_id=key.session_id,
                    agent_id=key.agent_id,
                    run_id="r2",
                    root_run_id="r2",
                    messages=[msg_r2],
                )
            )
            await eng.create(
                AgentRunActionModel.create_run_end(
                    user_id=key.user_id,
                    session_id=key.session_id,
                    agent_id=key.agent_id,
                    run_id="r2",
                    root_run_id="r2",
                    status="ok",
                )
            )

            # /rewind → undo before r2
            await eng.create(
                AgentRunActionModel.create_undo(
                    user_id=key.user_id,
                    session_id=key.session_id,
                    agent_id=key.agent_id,
                    run_id="r_undo",
                    root_run_id="r_undo",
                    undo_before_run_id="r2",
                    reason="user_rewind",
                )
            )

            state = await svc.load_messages(key=key)
            # State restored to before r2 → r1's content only
            assert _texts(state) == _texts([msg_r1])
            assert ("assistant", "r2 content (will be undone)") not in _texts(state)

    asyncio.run(run())


# ============================================================================
# Phase 2 iter-level idempotency
# ============================================================================


def test_iter_level_idempotency_unique_constraint():
    """Phase 2 streaming consumers redeliver actions on retry; UNIQUE blocks dups.

    Each iter-level APPEND uses ``idempotency_key="{run_id}:{iter_index}"``.
    A retry / Redis Consumer Group redelivery must not create duplicate rows.
    """

    async def run():
        async with _service() as (svc, eng):
            key = AgentRunActionKey(user_id="u", session_id="s_idem", agent_id="a")

            first = AgentRunActionModel.create_append(
                user_id=key.user_id,
                session_id=key.session_id,
                agent_id=key.agent_id,
                run_id="r1",
                root_run_id="r1",
                messages=[_msg("iter 0 result")],
                idempotency_key="r1:0",
            )
            await eng.create(first)

            # Simulated retry / redelivery
            duplicate = AgentRunActionModel.create_append(
                user_id=key.user_id,
                session_id=key.session_id,
                agent_id=key.agent_id,
                run_id="r1",
                root_run_id="r1",
                messages=[_msg("iter 0 result (dup)")],
                idempotency_key="r1:0",  # ← collision
            )
            with pytest.raises(IntegrityError):
                await eng.create(duplicate)

            # Only the first row exists; fold sees only first content
            actions = await _all_actions(eng, session_id=key.session_id, agent_id=key.agent_id)
            assert len(actions) == 1
            state = await svc.load_messages(key=key)
            assert _texts(state) == _texts([_msg("iter 0 result")])

    asyncio.run(run())


# ============================================================================
# Per-agent fold scope (multi-agent isolation)
# ============================================================================


def test_per_agent_fold_scope_isolation():
    """RFC §未解决问题 #1 (deferred): agent_id filter prevents cross-agent pollution.

    Two agents writing into the same session must produce isolated message
    states when fold-scoped to agent_id. Without this isolation, sub-agent
    actions would leak into parent agent's history.
    """

    async def run():
        async with _service() as (svc, eng):
            session_id = "s_multi"
            parent_msg = _msg("parent content")
            child_msg = _msg("child content (sub-agent internal)")

            # Parent agent's APPEND
            await eng.create(
                AgentRunActionModel.create_append(
                    user_id="u",
                    session_id=session_id,
                    agent_id="parent",
                    run_id="r_p",
                    root_run_id="r_p",
                    messages=[parent_msg],
                )
            )

            # Child agent's APPEND (sub-agent internal state)
            await eng.create(
                AgentRunActionModel.create_append(
                    user_id="u",
                    session_id=session_id,
                    agent_id="child",
                    run_id="r_c",
                    root_run_id="r_p",
                    parent_run_id="r_p",
                    messages=[child_msg],
                )
            )

            parent_state = await svc.load_messages(key=AgentRunActionKey(user_id="u", session_id=session_id, agent_id="parent"))
            child_state = await svc.load_messages(key=AgentRunActionKey(user_id="u", session_id=session_id, agent_id="child"))

            assert _texts(parent_state) == _texts([parent_msg])
            assert _texts(child_state) == _texts([child_msg])

            # No cross-pollution
            assert _texts([child_msg])[0] not in _texts(parent_state)
            assert _texts([parent_msg])[0] not in _texts(child_state)

    asyncio.run(run())


# ============================================================================
# Phase 1 backward compatibility:
# AgentRunActionService.load_messages already handles APPEND/UNDO/REPLACE,
# and compaction is Class B aliased onto REPLACE so it works automatically
# without requiring a Phase 3 type-recognition update.
# ============================================================================


def test_existing_service_load_messages_phase1_compat():
    """The existing ``AgentRunActionService.load_messages`` must continue to
    work for APPEND/UNDO/REPLACE flows after Phase 1 schema additions, and
    must correctly fold compaction (Class B REPLACE + reason='compact_*').
    """

    async def run():
        async with _service() as (svc, eng):
            key = AgentRunActionKey(user_id="u", session_id="s_compat", agent_id="a")

            # Use existing service API for APPEND
            user_msg = _msg("user q", role=Role.USER)
            asst_msg = _msg("assistant a")
            await svc.persist_append(
                key=key,
                run_id="r1",
                root_run_id="r1",
                parent_run_id=None,
                agent_name="a",
                messages=[user_msg, asst_msg],
            )

            # Compaction = REPLACE + reason='compact_auto'. The existing
            # service handles it as a plain REPLACE — that IS the Class B
            # aliasing guarantee.
            compacted = _msg("compacted")
            await eng.create(
                AgentRunActionModel.create_replace(
                    user_id=key.user_id,
                    session_id=key.session_id,
                    agent_id=key.agent_id,
                    run_id="r_compact",
                    root_run_id="r_compact",
                    messages=[compacted],
                    reason="compact_auto",
                )
            )

            # load_messages applies REPLACE → state == [compacted]
            loaded = await svc.load_messages(key=key)
            assert _texts(loaded) == _texts([compacted])

    asyncio.run(run())
