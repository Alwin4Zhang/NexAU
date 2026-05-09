# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""RFC-0022 Phase 2 — RUN_END must persist on the cancellation path.

Real-world signal: when an SSE client disconnects mid-run, RFC-0001 stop
plumbing cancels the agent task with ``asyncio.CancelledError``. Python's
asyncio re-raises ``CancelledError`` *immediately* on any bare ``await`` in
a finally block whose task is already in cancelled state — so a naive
``await persist_run_end(...)`` in finally **never executes**. The result
is an orphan RUN_START with no matching RUN_END.

Real-data confirmation: in local dev a chat run that succeeded but had its
SSE stream closed (RFC-0001 disconnect-stop path) ended with
``RUN_FINISHED`` event emitted but no RUN_END row in DB. The fix is
``asyncio.shield`` to protect the persist coroutine from outer
cancellation (it then runs to completion on the loop even after the
parent task ends).

This test pins the contract: even when the awaiter is cancelled, the
shielded persist task must complete and the RUN_END row must land.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from nexau.archs.session.agent_run_action_service import AgentRunActionKey, AgentRunActionService
from nexau.archs.session.models.agent_run_action_model import AgentRunActionModel, RunActionType
from nexau.archs.session.orm.filters import AndFilter, ComparisonFilter
from nexau.archs.session.orm.sql_engine import SQLDatabaseEngine


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


def test_run_end_lands_when_outer_task_is_cancelled():
    """Mimic the production cancellation path: a worker task awaits the
    shielded persist_run_end while a parent cancels it. The shielded coro
    must still complete and the RUN_END row must land in DB."""

    async def go():
        async with _service() as (svc, eng):
            persist_coro = svc.persist_run_end(
                key=_KEY,
                run_id="r1",
                root_run_id="r1",
                agent_name="a",
                status="ok",
            )

            async def shielded_awaiter() -> None:
                # This is what agent.py's finally does (in shape):
                #   await asyncio.wait_for(asyncio.shield(persist_coro), timeout=5.0)
                # If the awaiter is cancelled mid-await, the shielded coro
                # keeps running on the loop and lands the RUN_END row.
                try:
                    await asyncio.wait_for(asyncio.shield(persist_coro), timeout=5.0)
                except asyncio.CancelledError:
                    # Re-raise: caller still sees cancellation
                    raise

            task = asyncio.create_task(shielded_awaiter())
            # Yield once so task starts, then cancel
            await asyncio.sleep(0)
            task.cancel()

            # The awaiter raises CancelledError on the cancellation
            try:
                await task
                cancelled = False
            except asyncio.CancelledError:
                cancelled = True
            assert cancelled, "shielded awaiter must propagate cancellation to caller"

            # But the shielded inner coro continues — give the loop a moment
            # to flush it. In real code this happens on the agent-runtime
            # event loop which lives across requests.
            for _ in range(50):
                await asyncio.sleep(0)
                rows = await _all_actions(eng)
                if any(r.action_type == RunActionType.RUN_END for r in rows):
                    break

            rows = await _all_actions(eng)
            run_end_rows = [r for r in rows if r.action_type == RunActionType.RUN_END]
            assert len(run_end_rows) == 1, f"RUN_END must land even when awaiter was cancelled (got {len(run_end_rows)} rows)"

    asyncio.run(go())


def test_run_end_lands_in_normal_path():
    """Sanity: without cancellation, the shielded await completes normally
    and RUN_END lands as expected."""

    async def go():
        async with _service() as (svc, eng):
            persist_coro = svc.persist_run_end(
                key=_KEY,
                run_id="r2",
                root_run_id="r2",
                agent_name="a",
                status="ok",
            )
            await asyncio.wait_for(asyncio.shield(persist_coro), timeout=5.0)

            rows = await _all_actions(eng)
            assert len(rows) == 1
            assert rows[0].action_type == RunActionType.RUN_END

    asyncio.run(go())
