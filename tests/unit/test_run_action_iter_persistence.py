# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""RFC-0022 Phase 2 — iter-level + eager-user persistence.

The SDK previously batch-persisted at the END of agent.run() via a single
HistoryList.flush(). That meant any crash mid-run lost the user message
AND every completed iter's output. Phase 2 of RFC-0022 adds two flush
boundaries:

1. eager flush of the user_message right after it lands (in agent.py),
   before the iter loop starts
2. per-iter flush after each LLM iter completes (in executor.py via
   _persist_iter_progress)

The two together close the gap that produced ~6.6% of "actions truncated"
sessions in our parity scan against the test cluster.

This test file does NOT spin up a full Agent — it pokes the seams:

- persist_iter_progress flushes new iter messages as APPEND
- multiple consecutive iters produce multiple APPEND rows
- replay loads them all back as a single in-order message list
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

from nexau.archs.main_sub.execution.executor import Executor, _AsyncIterationState
from nexau.archs.main_sub.history_list import HistoryList
from nexau.archs.session.agent_run_action_service import AgentRunActionKey, AgentRunActionService
from nexau.archs.session.models.agent_run_action_model import AgentRunActionModel, RunActionType
from nexau.archs.session.orm.filters import AndFilter, ComparisonFilter
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


_KW: dict[str, Any] = dict(user_id="u", session_id="s", agent_id="a")
_KEY = AgentRunActionKey(**_KW)


def _msg(text: str, role: Role = Role.ASSISTANT) -> Message:
    return Message(role=role, content=[TextBlock(text=text)])


class _FakeSessionManager:
    """Minimal SessionManager stand-in exposing only what HistoryList touches."""

    def __init__(self, svc: AgentRunActionService) -> None:
        self.agent_run_action = svc


async def _all_actions(eng: SQLDatabaseEngine) -> list[AgentRunActionModel]:
    return await eng.find_many(
        AgentRunActionModel,
        filters=AndFilter(
            filters=[
                ComparisonFilter.eq("user_id", _KEY.user_id),
                ComparisonFilter.eq("session_id", _KEY.session_id),
                ComparisonFilter.eq("agent_id", _KEY.agent_id),
            ]
        ),
        order_by=("created_at_ns", "action_id"),
    )


# ============================================================================
# eager + per-iter shapes via direct HistoryList exercise
# ============================================================================


def test_eager_flush_persists_user_message_alone():
    """Mimic agent.py: append user_message → flush_async → DB has 1 APPEND
    with just the user message, BEFORE any iter has run."""

    async def go():
        async with _service() as (svc, eng):
            history = HistoryList(
                messages=[_msg("system", role=Role.SYSTEM)],
                session_manager=_FakeSessionManager(svc),  # type: ignore[arg-type]
                history_key=_KEY,
                run_id="r1",
                root_run_id="r1",
                agent_name="a",
            )
            history.append(_msg("hello", role=Role.USER))
            await history.flush_async()

            rows = await _all_actions(eng)
            assert len(rows) == 1
            assert rows[0].action_type == RunActionType.APPEND
            msgs = rows[0].append_messages or []
            assert len(msgs) == 1
            assert msgs[0]["role"] == "user"

    asyncio.run(go())


def test_per_iter_flush_emits_one_append_per_iter():
    """Each iter ends → its new messages flush as a single APPEND row.
    N iters → N+1 APPEND rows (N iters + 1 eager user)."""

    async def go():
        async with _service() as (svc, eng):
            history = HistoryList(
                messages=[],
                session_manager=_FakeSessionManager(svc),  # type: ignore[arg-type]
                history_key=_KEY,
                run_id="r1",
                root_run_id="r1",
                agent_name="a",
            )

            # Eager user
            history.append(_msg("question", role=Role.USER))
            await history.flush_async()

            # Iter 1: assistant + tool_result
            history.append(_msg("thinking...", role=Role.ASSISTANT))
            history.append(_msg("tool output 1", role=Role.TOOL))
            await history.flush_async()

            # Iter 2: another assistant turn
            history.append(_msg("thinking again...", role=Role.ASSISTANT))
            history.append(_msg("tool output 2", role=Role.TOOL))
            await history.flush_async()

            # Final iter: final assistant text
            history.append(_msg("here is the answer", role=Role.ASSISTANT))
            await history.flush_async()

            rows = await _all_actions(eng)
            # 1 eager + 3 iter flushes = 4 APPEND rows
            assert len(rows) == 4, f"expected 4 APPEND rows, got {len(rows)}"
            assert all(r.action_type == RunActionType.APPEND for r in rows)
            # Cumulative msg count: 1 + 2 + 2 + 1 = 6
            total_msgs = sum(len(r.append_messages or []) for r in rows)
            assert total_msgs == 6

    asyncio.run(go())


def test_crash_mid_iter_preserves_user_and_completed_iters():
    """Worst-case: agent crashes after iter 2 finishes its flush but before
    iter 3 starts. DB should have user + iter 1 + iter 2; iter 3 missing."""

    async def go():
        async with _service() as (svc, eng):
            history = HistoryList(
                messages=[],
                session_manager=_FakeSessionManager(svc),  # type: ignore[arg-type]
                history_key=_KEY,
                run_id="r1",
                root_run_id="r1",
                agent_name="a",
            )

            history.append(_msg("question", role=Role.USER))
            await history.flush_async()  # eager

            history.append(_msg("iter1 reply", role=Role.ASSISTANT))
            await history.flush_async()  # iter 1

            history.append(_msg("iter2 reply", role=Role.ASSISTANT))
            await history.flush_async()  # iter 2

            # Simulate crash here — iter 3 never persisted

            rows = await _all_actions(eng)
            assert len(rows) == 3
            all_msgs = []
            for r in rows:
                all_msgs.extend(r.append_messages or [])
            assert len(all_msgs) == 3
            assert all_msgs[0]["role"] == "user"
            assert all_msgs[1]["role"] == "assistant"
            assert all_msgs[2]["role"] == "assistant"

            # Replay: load_messages folds them back identically
            loaded = await svc.load_messages(key=_KEY)
            assert [m.role.value for m in loaded] == ["user", "assistant", "assistant"]

    asyncio.run(go())


# ============================================================================
# _persist_iter_progress no-op safety
# ============================================================================


def test_persist_iter_progress_noop_when_origin_history_not_history_list():
    """Sync execute() path passes a plain list as origin_history; the per-iter
    helper must short-circuit to avoid attribute errors."""

    async def go():
        executor = MagicMock(spec=Executor)
        # Bind the real method to the mock instance
        executor._persist_iter_progress = Executor._persist_iter_progress.__get__(executor)
        state = _AsyncIterationState(
            messages=[],
            final_response="",
            force_stop_reason=MagicMock(),
            iteration=0,
            agent_state=MagicMock(),
            token_trace_session=None,
            framework_context=MagicMock(),
            origin_history=[_msg("plain list", role=Role.USER)],  # plain list, not HistoryList
            runtime_client=None,
            custom_llm_client_provider=None,
        )
        # Should not raise
        await executor._persist_iter_progress(state)

    asyncio.run(go())
