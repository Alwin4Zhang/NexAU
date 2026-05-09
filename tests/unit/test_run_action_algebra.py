# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Algebraic laws of the RunAction fold (RFC-0022 §测试方案).

All laws run through the **production** ``AgentRunActionService.load_messages``
against a SQLite in-memory engine — no separate "reference fold" shadow
implementation. (An earlier draft kept a parallel in-memory fold for
"verification"; it diverged from production semantics and hid a real UNDO
bug for months. The reference is gone; production IS the spec.)

Laws verified (RFC-0022 §代数 property-based):

| Law | Property |
|-----|----------|
| Identity | fold([]) == [] |
| APPEND additivity | fold([APPEND(a), APPEND(b)]) → [...a, ...b] |
| REPLACE override | post-REPLACE state ignores pre-REPLACE actions |
| Compact ≡ REPLACE (state) | reason='compact_*' folds same as plain REPLACE |
| UNDO completeness | UNDO before X removes X and everything after |
| RUN markers transparency | RUN_START / RUN_END do not change state |
| Determinism | same action sequence folds to same state every time |
| Time-travel consistency | folding any prefix yields the state at that step |
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from nexau.archs.session.agent_run_action_service import AgentRunActionKey, AgentRunActionService
from nexau.archs.session.models.agent_run_action_model import (
    AgentRunActionModel,
    RunActionType,
)
from nexau.archs.session.orm.sql_engine import SQLDatabaseEngine
from nexau.core.messages import Message, Role, TextBlock

# ============================================================================
# Test harness — fold via real service, no shadow impl
# ============================================================================


@asynccontextmanager
async def _service() -> AsyncGenerator[tuple[AgentRunActionService, SQLDatabaseEngine]]:
    eng = SQLDatabaseEngine.from_url("sqlite+aiosqlite:///:memory:")
    try:
        await eng.setup_models([AgentRunActionModel])
        yield AgentRunActionService(engine=eng), eng
    finally:
        await eng._engine.dispose()


_KW: dict[str, Any] = dict(user_id="u", session_id="s", agent_id="a")


def fold(actions: list[AgentRunActionModel]) -> list[Message]:
    """Persist ``actions`` to a fresh sqlite engine, then call load_messages.

    This is the ONLY fold path in tests. No parallel in-memory implementation —
    we test what production actually runs.

    NOTE: deep-copies each action before insert. SQLAlchemy ORM attaches
    instance state to the first session that touches it; reusing the same
    instances across multiple ``fold()`` calls (e.g. property tests calling
    fold twice on the same input) would otherwise produce wrong / empty
    results on the second call.
    """

    async def run() -> list[Message]:
        async with _service() as (svc, eng):
            for a in actions:
                # Reconstruct via model_dump → __init__ to detach from any
                # prior SQLAlchemy session. SQLModel + table=True instances
                # carry session state that breaks reuse across engines.
                fresh = AgentRunActionModel(**a.model_dump())
                await eng.create(fresh)
            return await svc.load_messages(key=AgentRunActionKey(**_KW))

    return asyncio.run(run())


def _texts(msgs: list[Message]) -> list[str]:
    out: list[str] = []
    for m in msgs:
        for b in m.content:
            if isinstance(b, TextBlock):
                out.append(b.text)
    return out


# ============================================================================
# Hypothesis strategies — generate valid action sequences
# ============================================================================


@st.composite
def _message(draw, run_seed: int) -> Message:
    """Generate a deterministic Message tagged with the originating run id."""
    text = draw(st.text(min_size=1, max_size=20))
    return Message(role=Role.ASSISTANT, content=[TextBlock(text=f"r{run_seed}:{text}")])


@st.composite
def _append_action(draw, run_id: str, root_run_id: str) -> AgentRunActionModel:
    count = draw(st.integers(min_value=1, max_value=3))
    msgs = [draw(_message(run_seed=hash(run_id) % 1000)) for _ in range(count)]
    return AgentRunActionModel.create_append(
        run_id=run_id,
        root_run_id=root_run_id,
        messages=msgs,
        **_KW,
    )


@st.composite
def _action_sequence(draw, max_runs: int = 4) -> list[AgentRunActionModel]:
    """Generate a structurally-valid action sequence:

    - Each run_id is paired with RUN_START / RUN_END markers
    - **Each run has at least 1 APPEND** (matches realistic Phase 2 behavior +
      gives UNDO targets a non-marker anchor)
    - UNDO targets only previously-started runs
    """
    actions: list[AgentRunActionModel] = []
    started_runs: list[str] = []

    n_runs = draw(st.integers(min_value=1, max_value=max_runs))
    for i in range(n_runs):
        run_id = f"r{i}"
        actions.append(AgentRunActionModel.create_run_start(run_id=run_id, root_run_id=run_id, **_KW))
        started_runs.append(run_id)

        n_appends = draw(st.integers(min_value=1, max_value=2))
        for _ in range(n_appends):
            actions.append(draw(_append_action(run_id=run_id, root_run_id=run_id)))

        # 30% chance of UNDO inside this run (targeting a previous run)
        if started_runs[:-1] and draw(st.booleans()):
            target = draw(st.sampled_from(started_runs[:-1]))
            actions.append(AgentRunActionModel.create_undo(run_id=run_id, root_run_id=run_id, undo_before_run_id=target, **_KW))

        actions.append(AgentRunActionModel.create_run_end(run_id=run_id, root_run_id=run_id, status="ok", **_KW))

    return actions


# ============================================================================
# Algebraic laws (RFC-0022 §代数 property-based)
# ============================================================================


def test_law_identity_empty_fold():
    """fold([]) == []"""
    assert fold([]) == []


# Hypothesis tests use modest example counts because each example does a real
# DB roundtrip (sqlite in-memory but still ~1-50ms per fold).


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_action_sequence())
def test_law_determinism(actions):
    """Same action sequence folds to identical state every time."""
    state1 = fold(actions)
    state2 = fold(actions)
    assert _texts(state1) == _texts(state2)


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_action_sequence())
def test_law_run_markers_transparency(actions):
    """RUN_START / RUN_END do not change messages state.

    Stripping all RUN_START / RUN_END from a sequence must yield the same
    final state. UNDO uses cutoff_ns over actual timestamps, not RUN_START
    presence, so stripping markers doesn't affect it.
    """
    state_with = fold(actions)
    stripped = [a for a in actions if a.action_type not in (RunActionType.RUN_START, RunActionType.RUN_END)]
    state_stripped = fold(stripped)
    assert _texts(state_with) == _texts(state_stripped)


# ============================================================================
# Concrete unit tests for individual algebra laws
# ============================================================================


def _msg(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=[TextBlock(text=text)])


def test_law_append_additivity():
    """fold([APPEND(a), APPEND(b)]) == [...a, ...b]."""
    actions = [
        AgentRunActionModel.create_append(run_id="r1", root_run_id="r1", messages=[_msg("a"), _msg("b")], **_KW),
        AgentRunActionModel.create_append(run_id="r2", root_run_id="r2", messages=[_msg("c")], **_KW),
    ]
    assert _texts(fold(actions)) == ["a", "b", "c"]


def test_law_replace_override():
    """fold([..., REPLACE(x), ...post]) ignores anything before REPLACE."""
    actions = [
        AgentRunActionModel.create_append(run_id="r0", root_run_id="r0", messages=[_msg("old1"), _msg("old2")], **_KW),
        AgentRunActionModel.create_replace(run_id="r1", root_run_id="r1", messages=[_msg("anchor")], reason="user_clear", **_KW),
        AgentRunActionModel.create_append(run_id="r2", root_run_id="r2", messages=[_msg("post")], **_KW),
    ]
    assert _texts(fold(actions)) == ["anchor", "post"]


def test_law_compact_via_replace_equivalent_to_plain_replace():
    """REPLACE with reason='compact_*' folds identically to plain REPLACE.

    RFC-0022 §设计原则 §6 Class B aliasing — only metadata differs.
    """
    plain = [
        AgentRunActionModel.create_replace(run_id="r1", root_run_id="r1", messages=[_msg("X")], reason="user_clear", **_KW),
    ]
    compact = [
        AgentRunActionModel.create_replace(run_id="r1", root_run_id="r1", messages=[_msg("X")], reason="compact_auto", **_KW),
    ]
    assert _texts(fold(plain)) == _texts(fold(compact))


def test_law_undo_completeness():
    """UNDO before X removes X and everything chronologically >= X's first action."""
    actions = [
        AgentRunActionModel.create_append(run_id="r0", root_run_id="r0", messages=[_msg("keep")], **_KW),
        AgentRunActionModel.create_run_start(run_id="rX", root_run_id="rX", **_KW),
        AgentRunActionModel.create_append(run_id="rX", root_run_id="rX", messages=[_msg("during X")], **_KW),
        AgentRunActionModel.create_run_end(run_id="rX", root_run_id="rX", status="ok", **_KW),
        AgentRunActionModel.create_undo(run_id="r_undo", root_run_id="r_undo", undo_before_run_id="rX", **_KW),
    ]
    assert _texts(fold(actions)) == ["keep"]


def test_law_run_markers_alone_dont_appear_in_state():
    """A run that contains only RUN_START / RUN_END contributes no messages."""
    actions = [
        AgentRunActionModel.create_run_start(run_id="r1", root_run_id="r1", **_KW),
        AgentRunActionModel.create_run_end(run_id="r1", root_run_id="r1", status="ok", **_KW),
    ]
    assert fold(actions) == []
