# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""RFC-0022 Phase 2 — service-level RUN_START / RUN_END writes.

Covers:
- persist_run_start writes a RUN_START row with deterministic ``idempotency_key``
- persist_run_end writes a RUN_END row with the right status / reason
- Idempotency: a second persist with the same run_id is a silent no-op (returns
  None) — critical for retry / double-write safety
- Class A invariant: RUN_START + RUN_END do NOT change the fold output of
  ``load_messages`` (they are reader-NOOP markers)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from nexau.archs.session.agent_run_action_service import AgentRunActionKey, AgentRunActionService
from nexau.archs.session.models.agent_run_action_model import (
    AgentRunActionModel,
    RunActionType,
    RunEndExtra,
    RunStartExtra,
)
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
# RUN_START
# ============================================================================


def test_persist_run_start_writes_naked_marker_row():
    """Phase 2 wires RUN_START as a naked boundary marker — trace_id is the
    only consumer-facing field on RunStartExtra (RFC-0024); when absent the
    row carries just ``run_id`` + ``parent_run_id`` + ``created_at_ns``,
    which is all current consumers (call-tree boundary detection) need.
    """

    async def go():
        async with _service() as (svc, eng):
            row = await svc.persist_run_start(
                key=_KEY,
                run_id="r1",
                root_run_id="r1",
                agent_name="a1",
            )
            assert row is not None
            assert row.action_type == RunActionType.RUN_START
            assert row.idempotency_key == "r1:start"
            assert row.run_id == "r1"
            # Naked marker: extra is either None or an all-None RunStartExtra.
            extra = row.parse_extra()
            if extra is not None:
                assert isinstance(extra, RunStartExtra)
                assert extra.trace_id is None

    asyncio.run(go())


def test_persist_run_start_is_idempotent_on_same_run_id():
    """Calling persist_run_start twice with the same run_id must yield exactly
    one row in the DB; the second call returns None (caller-visible signal of
    "already happened") without raising."""

    async def go():
        async with _service() as (svc, eng):
            first = await svc.persist_run_start(key=_KEY, run_id="r1", root_run_id="r1", agent_name="a")
            second = await svc.persist_run_start(key=_KEY, run_id="r1", root_run_id="r1", agent_name="a")
            assert first is not None
            assert second is None  # idempotent skip
            rows = await _all_actions(eng)
            run_starts = [r for r in rows if r.action_type == RunActionType.RUN_START]
            assert len(run_starts) == 1

    asyncio.run(go())


# ============================================================================
# RUN_END
# ============================================================================


@pytest.mark.parametrize("status", ["ok", "error", "cancelled"])
def test_persist_run_end_writes_row_with_status(status: str):
    """status + reason are the consumer-visible value of RUN_END. trace_id
    is not populated (no consumer)."""

    async def go():
        async with _service() as (svc, eng):
            row = await svc.persist_run_end(
                key=_KEY,
                run_id="r1",
                root_run_id="r1",
                agent_name="a",
                status=status,  # type: ignore[arg-type]
                reason="something went wrong" if status != "ok" else None,
            )
            assert row is not None
            assert row.action_type == RunActionType.RUN_END
            assert row.idempotency_key == "r1:end"
            extra = row.parse_extra()
            assert isinstance(extra, RunEndExtra)
            assert extra.status == status
            assert extra.trace_id is None  # no consumer yet — left null by writer

    asyncio.run(go())


def test_persist_run_end_is_idempotent_on_same_run_id():
    async def go():
        async with _service() as (svc, eng):
            first = await svc.persist_run_end(key=_KEY, run_id="r1", root_run_id="r1", agent_name="a", status="ok")
            second = await svc.persist_run_end(
                key=_KEY,
                run_id="r1",
                root_run_id="r1",
                agent_name="a",
                status="error",  # status DIFFERS — first write still wins (idempotent on key)
            )
            assert first is not None
            assert second is None
            rows = await _all_actions(eng)
            run_ends = [r for r in rows if r.action_type == RunActionType.RUN_END]
            assert len(run_ends) == 1
            extra = run_ends[0].parse_extra()
            assert isinstance(extra, RunEndExtra)
            assert extra.status == "ok"  # first write preserved

    asyncio.run(go())


# ============================================================================
# Class A invariant — markers don't affect fold output
# ============================================================================


def test_run_markers_do_not_change_fold_output():
    """RUN_START / RUN_END are Class A (Reader-NOOP). Folding a sequence with
    them must produce the SAME message list as folding without them."""

    async def go():
        async with _service() as (svc, eng):
            # Without markers
            await svc.persist_append(
                key=_KEY,
                run_id="r1",
                root_run_id="r1",
                parent_run_id=None,
                agent_name="a",
                messages=[Message(role=Role.USER, content=[TextBlock(text="q1")])],
            )
            await svc.persist_append(
                key=_KEY,
                run_id="r1",
                root_run_id="r1",
                parent_run_id=None,
                agent_name="a",
                messages=[Message(role=Role.ASSISTANT, content=[TextBlock(text="a1")])],
            )
            without_markers = await svc.load_messages(key=_KEY)

        # Same scenario, same DB-fresh, with markers wrapping the appends
        async with _service() as (svc2, eng2):
            await svc2.persist_run_start(key=_KEY, run_id="r1", root_run_id="r1", agent_name="a")
            await svc2.persist_append(
                key=_KEY,
                run_id="r1",
                root_run_id="r1",
                parent_run_id=None,
                agent_name="a",
                messages=[Message(role=Role.USER, content=[TextBlock(text="q1")])],
            )
            await svc2.persist_append(
                key=_KEY,
                run_id="r1",
                root_run_id="r1",
                parent_run_id=None,
                agent_name="a",
                messages=[Message(role=Role.ASSISTANT, content=[TextBlock(text="a1")])],
            )
            await svc2.persist_run_end(key=_KEY, run_id="r1", root_run_id="r1", agent_name="a", status="ok")
            with_markers = await svc2.load_messages(key=_KEY)

        # Texts must match exactly (ids may differ since they're auto-generated
        # per Message instance, but role + text content drive equivalence here)
        def _signature(msgs: list[Message]) -> list[tuple[str, str]]:
            out: list[tuple[str, str]] = []
            for m in msgs:
                txt = "".join(b.text for b in m.content if isinstance(b, TextBlock))
                out.append((m.role.value, txt))
            return out

        assert _signature(without_markers) == _signature(with_markers)

    asyncio.run(go())


def test_persist_run_start_works_for_subagent():
    """parent_run_id is optional; verify sub-agent path persists with it."""

    async def go():
        async with _service() as (svc, eng):
            row = await svc.persist_run_start(
                key=_KEY,
                run_id="sub-1",
                root_run_id="root-1",
                parent_run_id="parent-1",
                agent_name="sub-agent",
            )
            assert row is not None
            assert row.parent_run_id == "parent-1"
            assert row.root_run_id == "root-1"
            assert row.run_id == "sub-1"

    asyncio.run(go())
