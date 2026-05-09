"""RFC-0026 — HookResult.history_event single-write-site invariants.

Two things this file locks down:

1. ``HookResult.history_event`` is a typed-event slot that flows through
   the existing HookResult channel — additive, no breaking change to
   middleware that doesn't set it. Discriminated union (HistoryEvent =
   ReplaceEvent | AppendEvent | UndoEvent | UnknownEvent) so future event
   types don't churn the schema.

2. ``HistoryList.replace_all(messages, replace_extra=variant)`` schedules a
   typed REPLACE persist write AND realigns the baseline so the next
   ``flush()`` doesn't double-write an untyped REPLACE.

(Phase 3 era ``emit_typed_replace`` / ``adopt_replaced_state`` were
deleted entirely as part of this PR's RFC-0026 work — no external
caller exists since they only ever shipped to intermediate commits
of this PR.)
"""

from __future__ import annotations

import asyncio

from nexau.archs.main_sub.execution.history_events import ReplaceEvent
from nexau.archs.main_sub.execution.hooks import (
    AfterModelHookInput,
    BeforeModelHookInput,
    HookResult,
    Middleware,
    MiddlewareManager,
)
from nexau.archs.main_sub.history_list import HistoryList
from nexau.archs.session import AgentRunActionKey, SessionManager
from nexau.archs.session.models.agent_run_action_model import (
    AgentRunActionModel,
    CompactAutoVariant,
    CompactStats,
    RunActionType,
)
from nexau.archs.session.orm.filters import ComparisonFilter
from nexau.archs.session.orm.sql_engine import SQLDatabaseEngine
from nexau.core.messages import Message, Role, TextBlock


def _sys() -> Message:
    return Message(role=Role.SYSTEM, content=[TextBlock(text="sys")])


# ---------------------------------------------------------------------------
# 1. HookResult schema — additive, default None
# ---------------------------------------------------------------------------


def test_hook_result_history_event_defaults_to_none():
    """Existing callers that never set history_event are unaffected."""
    r = HookResult.with_modifications(messages=[Message.user("hi")])
    assert r.history_event is None
    assert r.messages is not None and len(r.messages) == 1


def test_hook_result_history_event_round_trips_replace_event():
    variant = CompactAutoVariant(strategy="sliding_window", stats=CompactStats(pre_message_count=10, post_message_count=3))
    event = ReplaceEvent(messages=[Message.user("compacted")], extra=variant)
    r = HookResult.with_modifications(messages=[Message.user("compacted")], history_event=event)
    assert r.history_event is event
    assert isinstance(r.history_event, ReplaceEvent)
    assert r.history_event.extra is variant


# ---------------------------------------------------------------------------
# 2. MiddlewareManager funnels HookResult.history_event into hook_input outparam
# ---------------------------------------------------------------------------


class _CompactingMiddleware(Middleware):
    """Stub: returns a typed REPLACE event on before_model."""

    def __init__(self, event: ReplaceEvent):
        super().__init__()
        self._event = event

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        return HookResult.with_modifications(messages=[Message.user("compacted!")], history_event=self._event)


class _NoopMiddleware(Middleware):
    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        return HookResult.no_changes()

    def after_model(self, hook_input: AfterModelHookInput) -> HookResult:
        return HookResult.no_changes()


def test_middleware_manager_publishes_history_event_to_hook_input():
    variant = CompactAutoVariant(strategy="test", stats=CompactStats(pre_message_count=5, post_message_count=1))
    event = ReplaceEvent(messages=[Message.user("compacted!")], extra=variant)
    mgr = MiddlewareManager([_NoopMiddleware(), _CompactingMiddleware(event)])
    hook_input = BeforeModelHookInput(
        agent_state=None,  # type: ignore[arg-type]  # not used by these stubs
        max_iterations=10,
        current_iteration=0,
        messages=[Message.user("orig")],
    )
    new_messages = mgr.run_before_model(hook_input)
    assert new_messages[0].get_text_content() == "compacted!"
    # Outparam: executor reads this immediately after the chain returns and
    # dispatches by event type (ReplaceEvent → ctx.history.replace).
    assert hook_input.history_event is event


def test_middleware_manager_clears_history_event_at_start_of_run():
    """Stale state from a prior run must NOT leak through."""
    mgr = MiddlewareManager([_NoopMiddleware()])
    stale_event = ReplaceEvent(
        messages=[Message.user("stale")],
        extra=CompactAutoVariant(strategy="stale", stats=None),
    )
    hook_input = BeforeModelHookInput(
        agent_state=None,  # type: ignore[arg-type]
        max_iterations=10,
        current_iteration=0,
        messages=[Message.user("hi")],
        history_event=stale_event,
    )
    mgr.run_before_model(hook_input)
    assert hook_input.history_event is None  # cleared


def test_history_event_unknown_type_decodes_to_unknown_event_for_forward_compat():
    """RFC-0026 forward-compat: a future SDK that emits an event with an
    unknown ``type`` discriminator must decode cleanly in older SDK code
    as ``UnknownEvent`` (executor silently skips), not raise ValidationError.

    This locks in the discriminated-union ``UnknownEvent`` fallback so
    nobody accidentally narrows the discriminator to ``Literal[...]``
    and breaks rolling upgrades.
    """
    from pydantic import TypeAdapter

    from nexau.archs.main_sub.execution.history_events import HistoryEvent, UnknownEvent

    adapter = TypeAdapter(HistoryEvent)
    decoded = adapter.validate_python({"type": "checkpoint", "some_future_field": "x"})
    assert isinstance(decoded, UnknownEvent)
    assert decoded.type == "checkpoint"


# ---------------------------------------------------------------------------
# 3. HistoryList.replace_all(replace_extra=...) writes typed REPLACE row
# ---------------------------------------------------------------------------


def _make_history(tmp_db: str) -> tuple[HistoryList, SessionManager, str]:
    """Build a HistoryList wired to a fresh SQLite session-manager."""
    engine = SQLDatabaseEngine.from_url(f"sqlite+aiosqlite:///{tmp_db}")

    async def setup():
        await engine.setup_models([AgentRunActionModel])

    asyncio.run(setup())
    sm = SessionManager(engine=engine)
    key = AgentRunActionKey(user_id="u1", session_id="s1", agent_id="a1")
    history = HistoryList(
        messages=[_sys()],
        session_manager=sm,
        history_key=key,
        run_id="r1",
        root_run_id="r1",
        agent_name="testagent",
    )
    return history, sm, str(engine._engine.url)


def _read_actions(sm: SessionManager, agent_id: str, session_id: str = "s1") -> list[AgentRunActionModel]:
    async def go():
        return await sm.agent_run_action._engine.find_many(
            AgentRunActionModel,
            filters=ComparisonFilter.eq("agent_id", agent_id),
        )

    return asyncio.run(go())


def test_replace_all_with_replace_extra_writes_typed_replace_row(tmp_path):
    db = tmp_path / "rfc0026.db"
    history, sm, _ = _make_history(str(db))
    variant = CompactAutoVariant(
        strategy="sliding_window",
        stats=CompactStats(pre_message_count=5, post_message_count=2),
    )

    new_msgs = [_sys(), Message.user("kept-after-compact")]

    async def go():
        history.replace_all(new_msgs, replace_extra=variant)
        # Drain any background tasks scheduled by the persist write.
        await asyncio.sleep(0.05)

    asyncio.run(go())

    rows = _read_actions(sm, "a1")
    replace_rows = [r for r in rows if r.action_type == RunActionType.REPLACE]
    assert len(replace_rows) == 1, f"expected exactly one REPLACE row, got {len(replace_rows)} (all: {[r.action_type for r in rows]})"
    extra = replace_rows[0].parse_extra()
    assert isinstance(extra, CompactAutoVariant), f"expected CompactAutoVariant, got {type(extra).__name__}"
    assert extra.strategy == "sliding_window"
    assert extra.stats is not None and extra.stats.pre_message_count == 5


def test_replace_all_without_replace_extra_keeps_existing_diff_path(tmp_path):
    """No replace_extra → no eager write; flush() handles it via fingerprint diff."""
    db = tmp_path / "rfc0026_no_extra.db"
    history, sm, _ = _make_history(str(db))

    async def go():
        history.replace_all([_sys(), Message.user("changed")])
        await asyncio.sleep(0.05)

    asyncio.run(go())

    rows = _read_actions(sm, "a1")
    # No eager write. flush() not called → no rows. This is the existing
    # behavior we MUST preserve (no breaking change for callers that pass
    # only messages).
    assert len(rows) == 0, f"replace_all without replace_extra should not write; got {len(rows)} rows"


# ---------------------------------------------------------------------------
# 4. FrameworkContext.history.replace — RPC-friendly typed-event API
# ---------------------------------------------------------------------------


def test_framework_context_history_replace_writes_typed_replace_row(tmp_path):
    """RFC-0026: ctx.history.replace(messages, extra=variant) is the public
    write-side typed-event API. Internally routes to HistoryList.replace_all
    with replace_extra so a typed REPLACE row lands on disk."""
    import threading

    from nexau.archs.main_sub.framework_context import FrameworkContext
    from nexau.archs.tool.tool_registry import ToolRegistry

    db = tmp_path / "rfc0026_ctx.db"
    history, sm, _ = _make_history(str(db))

    ctx = FrameworkContext(
        agent_name="a1",
        agent_id="a1",
        run_id="r1",
        root_run_id="r1",
        _tool_registry=ToolRegistry(),
        _shutdown_event=threading.Event(),
        _history=history,
    )
    variant = CompactAutoVariant(
        strategy="ctx_history_replace_test",
        stats=CompactStats(pre_message_count=8, post_message_count=2),
    )

    async def go():
        ctx.history.replace([_sys(), Message.user("via ctx.history.replace")], extra=variant)
        await asyncio.sleep(0.05)

    asyncio.run(go())

    rows = _read_actions(sm, "a1")
    replace_rows = [r for r in rows if r.action_type == RunActionType.REPLACE]
    assert len(replace_rows) == 1
    extra = replace_rows[0].parse_extra()
    assert isinstance(extra, CompactAutoVariant)
    assert extra.strategy == "ctx_history_replace_test"


def test_framework_context_history_replace_no_op_without_history():
    """ctx.history.replace must be a safe no-op when no HistoryList is wired
    (in-process tests, no SessionManager). Mirrors HistoryAPI's RPC-future
    semantics: failing silently when there's no backend to call."""
    import threading

    from nexau.archs.main_sub.framework_context import FrameworkContext
    from nexau.archs.tool.tool_registry import ToolRegistry

    ctx = FrameworkContext(
        agent_name="t",
        agent_id="t",
        run_id="r",
        root_run_id="r",
        _tool_registry=ToolRegistry(),
        _shutdown_event=threading.Event(),
        # _history defaults to None
    )
    variant = CompactAutoVariant(strategy="noop", stats=None)

    # Must not raise.
    ctx.history.replace([_sys()], extra=variant)


def test_for_tool_call_propagates_history_handle(tmp_path):
    """RFC-0019 + RFC-0026: per-tool-call FrameworkContext clones must
    carry the same history handle so tool-spawned writers see the same
    backing HistoryList."""
    import threading

    from nexau.archs.main_sub.framework_context import FrameworkContext
    from nexau.archs.tool.tool_registry import ToolRegistry

    db = tmp_path / "rfc0026_for_tool.db"
    history, _, _ = _make_history(str(db))

    parent_ctx = FrameworkContext(
        agent_name="a",
        agent_id="a",
        run_id="r",
        root_run_id="r",
        _tool_registry=ToolRegistry(),
        _shutdown_event=threading.Event(),
        _history=history,
    )
    child_ctx = parent_ctx.for_tool_call(tool_name="bash", allow_rules=["**"], deny_rules=[])
    assert child_ctx.history._history is history
