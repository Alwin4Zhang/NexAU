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


# ============================================================================
# RFC-0022 Phase 2: iter_index + idempotency_key plumbing (PR #547)
# ============================================================================


def test_flush_async_with_iter_index_writes_idempotency_key_and_extra():
    """flush_async(iter_index=N) → APPEND row carries iter_index in extra
    AND idempotency_key=f"{run_id}:{N}". Verifies the per-iter plumbing
    end-to-end: HistoryList → AgentRunActionService → DB."""

    async def go():
        async with _service() as (svc, eng):
            history = HistoryList(
                messages=[_msg("system", role=Role.SYSTEM)],
                session_manager=_FakeSessionManager(svc),  # type: ignore[arg-type]
                history_key=_KEY,
                run_id="r_plumb",
                root_run_id="r_plumb",
                agent_name="a",
            )
            history.append(_msg("user msg", role=Role.USER))
            await history.flush_async(iter_index=0)

            history.append(_msg("assistant iter 1"))
            await history.flush_async(iter_index=1)

            rows = await _all_actions(eng)
            assert len(rows) == 2
            assert rows[0].idempotency_key == "r_plumb:0"
            assert rows[1].idempotency_key == "r_plumb:1"
            assert rows[0].extra == {"iter_index": 0}
            assert rows[1].extra == {"iter_index": 1}

    asyncio.run(go())


def test_flush_async_no_iter_index_keeps_key_null():
    """flush_async() with no iter_index → batch-mode behaviour preserved:
    idempotency_key=NULL, extra=NULL. Phase 1 callers / end-of-run trailing
    flush must not regress."""

    async def go():
        async with _service() as (svc, eng):
            history = HistoryList(
                session_manager=_FakeSessionManager(svc),  # type: ignore[arg-type]
                history_key=_KEY,
                run_id="r_batch",
                root_run_id="r_batch",
                agent_name="a",
            )
            history.append(_msg("user", role=Role.USER))
            history.append(_msg("assistant"))
            await history.flush_async()  # no iter_index

            rows = await _all_actions(eng)
            assert len(rows) == 1
            assert rows[0].idempotency_key is None
            assert rows[0].extra is None

    asyncio.run(go())


def test_persist_append_idempotent_collapse_on_unique_violation():
    """Two persist_append with same idempotency_key → second returns None
    (idempotent collapse). UNIQUE collision is caught and logged, not raised.

    Models the retry / Consumer Group redelivery scenario where the same
    iter's flush is attempted twice (e.g. caller hit a transient timeout,
    retried, but the first write actually succeeded)."""

    async def go():
        async with _service() as (svc, eng):
            first = await svc.persist_append(
                key=_KEY,
                run_id="r_idem",
                root_run_id="r_idem",
                parent_run_id=None,
                agent_name="a",
                messages=[_msg("first write")],
                iter_index=0,
                idempotency_key="r_idem:0",
            )
            assert first is not None
            assert first.idempotency_key == "r_idem:0"

            second = await svc.persist_append(
                key=_KEY,
                run_id="r_idem",
                root_run_id="r_idem",
                parent_run_id=None,
                agent_name="a",
                messages=[_msg("retry — same iter")],
                iter_index=0,
                idempotency_key="r_idem:0",
            )
            assert second is None  # collapsed

            rows = await _all_actions(eng)
            assert len(rows) == 1  # no duplicate
            msgs = rows[0].append_messages or []
            assert msgs[0].get_text_content() == "first write"  # original wins

    asyncio.run(go())


# ============================================================================
# Forward / backward data compatibility (RFC-0022 §设计原则 §6 protobuf evolution)
#
# AppendExtra schema changed in PR #547 (iter_kind + llm_call_id removed,
# iter_index kept). PROTOBUF_PHILOSOPHY (extra='allow') guarantees:
#
#   - New reader on old data: dropped fields land in the catchall, current
#     AppendExtra surfaces only the live field set (trace_id + iter_index).
#   - Old reader on new data: a hypothetical pre-#547 AppendExtra (without
#     iter_index) parses new rows without crashing — the new field passes
#     through extra='allow' as unknown.
#
# These tests freeze the contract so future schema deletions can't silently
# break readers running an older SDK against a newer DB (or vice versa).
# ============================================================================


def test_compat_new_reader_on_old_extra_with_dropped_fields():
    """A row written by old SDK has extra={iter_kind, llm_call_id, ...} —
    current AppendExtra (post-#547) parses it cleanly, ignores the dropped
    fields, surfaces the live ones.
    """
    from nexau.archs.session.models.agent_run_action_model import AppendExtra

    # Mimics what an old (pre-#547) writer would have stamped:
    legacy_extra = {
        "iter_index": 7,
        "iter_kind": "tool_round",  # field removed in #547
        "llm_call_id": "msg_legacy_01ABC",  # field removed in #547
        "trace_id": "abc123",
    }

    parsed = AppendExtra.model_validate(legacy_extra)

    # Live fields surfaced as typed attrs (mypy / IDE autocomplete sees these).
    assert parsed.iter_index == 7
    assert parsed.trace_id == "abc123"

    # Dropped fields ARE still reachable as plain attributes thanks to
    # ``extra='allow'`` — pydantic stores unknown fields on the instance.
    # This is intentional: it means callers that haven't recompiled against
    # the new SDK don't lose access to their old fields. The tradeoff is
    # that mypy / IDE won't autocomplete them anymore (correct).
    assert getattr(parsed, "iter_kind") == "tool_round"  # noqa: B009
    assert getattr(parsed, "llm_call_id") == "msg_legacy_01ABC"  # noqa: B009

    # __pydantic_extra__ is the canonical access path for unknown fields.
    extras_pocket = getattr(parsed, "__pydantic_extra__", None) or {}
    assert extras_pocket.get("iter_kind") == "tool_round"
    assert extras_pocket.get("llm_call_id") == "msg_legacy_01ABC"

    # Round-trip preserves the unknown fields too — JSONB-stored row stays
    # forensically intact even if it passes through a parse/dump cycle in a
    # newer reader.
    dumped = parsed.model_dump(exclude_none=True)
    assert dumped["iter_kind"] == "tool_round"
    assert dumped["llm_call_id"] == "msg_legacy_01ABC"


def test_compat_old_reader_shape_on_new_extra_with_iter_index():
    """A pre-#547 SDK would have had AppendExtra without iter_index. Simulate
    that older shape and prove the new ``{"iter_index": N, "trace_id": "..."}``
    payload doesn't crash it — protobuf-philosophy forward-compat: unknown
    fields land in extra='allow' catchall, known fields surface normally.
    """
    from pydantic import BaseModel, ConfigDict

    class LegacyAppendExtra(BaseModel):
        """Mimics AppendExtra as it existed before PR #547 added iter_index."""

        model_config = ConfigDict(extra="allow")
        trace_id: str | None = None
        # iter_index intentionally absent — this is the "old" reader shape

    new_extra_payload = {
        "iter_index": 12,  # field added in #547
        "trace_id": "def456",
    }

    parsed = LegacyAppendExtra.model_validate(new_extra_payload)

    # Known field surfaces.
    assert parsed.trace_id == "def456"

    # Unknown new field reachable via catchall, doesn't raise.
    extras_pocket = getattr(parsed, "__pydantic_extra__", None) or {}
    assert extras_pocket.get("iter_index") == 12


def test_compat_round_trip_legacy_extra_to_db_preserves_pocket():
    """End-to-end: write a row with legacy-shape extra to the DB, read back,
    parse_extra returns AppendExtra; the dropped fields persist in the JSONB
    column so a forensic reader can still see them — only the typed accessor
    skips them.
    """
    import asyncio

    async def go():
        async with _service() as (svc, eng):
            # Write directly via factory (bypass create_append's typed extra
            # path) to inject a legacy-shape extra dict — what an old SDK
            # would have produced.
            from nexau.archs.session.models.agent_run_action_model import (
                AgentRunActionModel,
                AppendExtra,
                RunActionType,
            )

            legacy_extra = {
                "iter_index": 4,
                "iter_kind": "tool_round",
                "llm_call_id": "msg_legacy",
                "trace_id": "trace_legacy",
            }
            row = AgentRunActionModel(
                user_id="u",
                session_id="s",
                agent_id="a",
                run_id="r_legacy",
                root_run_id="r_legacy",
                action_type=RunActionType.APPEND,
                append_messages=[_msg("legacy hello")],
                idempotency_key="r_legacy:4",
                extra=legacy_extra,
            )
            await eng.create(row)

            rows = await _all_actions(eng)
            assert len(rows) == 1
            stored = rows[0]

            # JSONB column preserves the full original dict (pocket lives on).
            assert stored.extra == legacy_extra

            # Typed accessor surfaces only live fields.
            parsed = stored.parse_extra()
            assert isinstance(parsed, AppendExtra)
            assert parsed.iter_index == 4
            assert parsed.trace_id == "trace_legacy"

    asyncio.run(go())
