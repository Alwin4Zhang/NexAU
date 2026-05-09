# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""RFC-0022 Phase 3 — typed REPLACE writes (service-level).

Validates ``AgentRunActionService.persist_replace(extra=...)`` carries
the typed variant through to the persisted action row (round-trip via
``parse_extra()``).

The HistoryList integration tests (``emit_typed_replace`` /
``adopt_replaced_state``) were removed in RFC-0026 — those methods are
gone; HistoryList integration is now covered by
``test_rfc0026_history_event_channel.py`` (HookResult.history_event +
ctx.history.replace path).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from nexau.archs.session.agent_run_action_service import AgentRunActionKey, AgentRunActionService
from nexau.archs.session.models.agent_run_action_model import (
    AgentRunActionModel,
    CompactAutoVariant,
    CompactFocusedVariant,
    CompactStats,
    RunActionType,
    UserClearVariant,
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


def _msg(text: str, role: Role = Role.ASSISTANT) -> Message:
    return Message(role=role, content=[TextBlock(text=text)])


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
# Service layer
# ============================================================================


def test_persist_replace_with_compact_auto_variant_round_trips():
    async def go():
        async with _service() as (svc, eng):
            variant = CompactAutoVariant(
                strategy="SlidingWindowCompaction",
                stats=CompactStats(
                    pre_message_count=20,
                    post_message_count=5,
                    pre_tokens=12000,
                    post_tokens=2500,
                ),
            )
            row = await svc.persist_replace(
                key=_KEY,
                run_id="r1",
                root_run_id="r1",
                messages=[_msg("compact summary")],
                extra=variant,
            )
            assert row.action_type == RunActionType.REPLACE
            extra = row.parse_extra()
            assert isinstance(extra, CompactAutoVariant)
            assert extra.strategy == "SlidingWindowCompaction"
            assert extra.stats is not None
            assert extra.stats.pre_message_count == 20
            assert extra.stats.post_message_count == 5
            assert extra.stats.pre_tokens == 12000
            assert extra.stats.post_tokens == 2500

    asyncio.run(go())


def test_persist_replace_with_compact_focused_variant_round_trips():
    async def go():
        async with _service() as (svc, eng):
            variant = CompactFocusedVariant(
                focus_instructions="Keep only error-handling discussion",
                strategy="LLMSummarizeCompaction",
            )
            row = await svc.persist_replace(
                key=_KEY,
                run_id="r1",
                root_run_id="r1",
                messages=[_msg("focused summary")],
                extra=variant,
            )
            extra = row.parse_extra()
            assert isinstance(extra, CompactFocusedVariant)
            assert extra.focus_instructions == "Keep only error-handling discussion"

    asyncio.run(go())


def test_persist_replace_without_extra_writes_untyped_replace():
    """Back-compat: callers who don't pass extra get a plain REPLACE row
    with no typed variant — exact pre-Phase-3 behavior."""

    async def go():
        async with _service() as (svc, eng):
            row = await svc.persist_replace(
                key=_KEY,
                run_id="r1",
                root_run_id="r1",
                messages=[_msg("no-reason replace")],
            )
            assert row.action_type == RunActionType.REPLACE
            # extra is None → parse_extra returns None
            assert row.parse_extra() is None

    asyncio.run(go())


def test_persist_replace_with_user_clear_variant_round_trips():
    async def go():
        async with _service() as (svc, eng):
            variant = UserClearVariant()
            row = await svc.persist_replace(
                key=_KEY,
                run_id="r1",
                root_run_id="r1",
                messages=[_msg("anchor")],
                extra=variant,
            )
            extra = row.parse_extra()
            assert isinstance(extra, UserClearVariant)
            assert extra.reason == "user_clear"

    asyncio.run(go())
