# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""T4 unit tests for Agent permission hard-block and resume.

RFC-0019: Agent 层权限管理测试

Covers:
- agent.run_async() raises PendingPermissionsError with unresolved decisions
- resolve_permission("allow") persists rule and marks decision
- resolve_permission("deny") marks decision without persisting rule
- resolve_permission("allow_once") marks decision without persisting rule
- Resume after all-allow: tools re-executed, ToolResults written
- Resume after mixed allow/deny: correct outcomes
- Idempotency: resume skips consumed entries
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexau.archs.main_sub.agent_context import AgentContext, GlobalStorage
from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.main_sub.framework_context import FrameworkContext
from nexau.archs.main_sub.history_list import HistoryList
from nexau.archs.permissions.types import PendingPermissionsError
from nexau.archs.tool.tool import Tool
from nexau.archs.tool.tool_registry import ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent_state() -> AgentState:
    return AgentState(
        agent_name="perm_agent",
        agent_id="perm_id",
        run_id="run_p1",
        root_run_id="run_p1",
        context=AgentContext({}),
        global_storage=GlobalStorage(),
        tool_registry=ToolRegistry(),
    )


def _make_framework_context(tool_registry: ToolRegistry | None = None) -> FrameworkContext:
    return FrameworkContext(
        agent_name="perm_agent",
        agent_id="perm_id",
        run_id="run_p1",
        root_run_id="run_p1",
        _tool_registry=tool_registry or ToolRegistry(),
        _shutdown_event=threading.Event(),
        session_id="sess_1",
    )


def _default_impl(**kwargs: object) -> dict[str, bool]:
    return {"ok": True}


def _make_tool(name: str, impl: Callable[..., Any] | None = None) -> Tool:
    if impl is None:
        impl = _default_impl
    return Tool(
        name=name,
        description=f"Test tool: {name}",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        implementation=impl,
    )


def _make_mock_session_manager(
    pending: dict[str, Any] | None = None,
) -> AsyncMock:
    sm = AsyncMock()
    sm.get_pending_tool_calls = AsyncMock(return_value=pending)
    sm.update_pending_tool_calls = AsyncMock()
    sm.save_permission_rule = AsyncMock()
    sm.agent_lock = MagicMock()
    sm.agent_lock.acquire = MagicMock()
    sm.setup_models = AsyncMock()
    return sm


def _make_pending_entry(
    tool_name: str,
    *,
    permission_key: str = "key:test",
    prompt: str = "Allow?",
    decision: str | None = None,
    consumed: bool = False,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "tool_name": tool_name,
        "prompt": prompt,
        "permission_key": permission_key,
        "parameters": {"x": "1"},
        "decision": decision,
    }
    if consumed:
        entry["consumed"] = True
    return entry


# ---------------------------------------------------------------------------
# Test resolve_permission
# ---------------------------------------------------------------------------


class TestResolvePermission:
    """Unit tests for Agent.resolve_permission."""

    @pytest.mark.anyio
    async def test_allow_persists_rule(self):
        """resolve_permission('allow') saves a permanent allow rule to DB."""
        from nexau.archs.main_sub.agent import Agent

        pending = {
            "tc_1": _make_pending_entry("write_file", permission_key="path:/tmp/out.txt"),
        }
        sm = _make_mock_session_manager(pending=pending)

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"

        await agent.resolve_permission("tc_1", "allow")

        sm.save_permission_rule.assert_called_once_with(
            user_id="u1",
            session_id="s1",
            tool_name="write_file",
            rule_content="path:/tmp/out.txt",
            behavior="allow",
            source="user",
        )

        sm.update_pending_tool_calls.assert_called_once()
        updated_pending = sm.update_pending_tool_calls.call_args[1]["pending_tool_calls"]
        assert updated_pending["tc_1"]["decision"] == "allow"

    @pytest.mark.anyio
    async def test_deny_does_not_persist_rule(self):
        """resolve_permission('deny') marks decision but does NOT save a rule."""
        from nexau.archs.main_sub.agent import Agent

        pending = {
            "tc_2": _make_pending_entry("run_shell", permission_key="shell:rm"),
        }
        sm = _make_mock_session_manager(pending=pending)

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"

        await agent.resolve_permission("tc_2", "deny")

        sm.save_permission_rule.assert_not_called()

        updated_pending = sm.update_pending_tool_calls.call_args[1]["pending_tool_calls"]
        assert updated_pending["tc_2"]["decision"] == "deny"

    @pytest.mark.anyio
    async def test_allow_once_does_not_persist_rule(self):
        """resolve_permission('allow_once') marks decision but does NOT save a permanent rule."""
        from nexau.archs.main_sub.agent import Agent

        pending = {
            "tc_3": _make_pending_entry("write_file", permission_key="path:/var"),
        }
        sm = _make_mock_session_manager(pending=pending)

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"

        await agent.resolve_permission("tc_3", "allow_once")

        sm.save_permission_rule.assert_not_called()

        updated_pending = sm.update_pending_tool_calls.call_args[1]["pending_tool_calls"]
        assert updated_pending["tc_3"]["decision"] == "allow_once"

    @pytest.mark.anyio
    async def test_unknown_tool_call_id_raises(self):
        """resolve_permission raises ValueError for unknown tool_call_id."""
        from nexau.archs.main_sub.agent import Agent

        pending = {"tc_known": _make_pending_entry("t")}
        sm = _make_mock_session_manager(pending=pending)

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"

        with pytest.raises(ValueError, match="tc_unknown"):
            await agent.resolve_permission("tc_unknown", "allow")

    @pytest.mark.anyio
    async def test_no_pending_raises(self):
        """resolve_permission raises ValueError when no pending exists."""
        from nexau.archs.main_sub.agent import Agent

        sm = _make_mock_session_manager(pending=None)

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"

        with pytest.raises(ValueError, match="tc_x"):
            await agent.resolve_permission("tc_x", "allow")


# ---------------------------------------------------------------------------
# Test _resume_pending_tool_calls
# ---------------------------------------------------------------------------


class TestResumePendingToolCalls:
    """Unit tests for Agent._resume_pending_tool_calls."""

    @pytest.mark.anyio
    async def test_deny_synthesizes_denial_tool_result(self):
        """Denied tool_call produces is_error=True ToolResult message in history."""
        from nexau.archs.main_sub.agent import Agent

        sm = _make_mock_session_manager()

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"
            agent._history = HistoryList([])
            agent.executor = MagicMock()
            agent.executor.tool_registry = ToolRegistry()

        pending = {
            "tc_d": _make_pending_entry("blocked_tool", decision="deny"),
        }

        agent_state = _make_agent_state()
        ctx = _make_framework_context()

        await agent._resume_pending_tool_calls(pending, agent_state, ctx)

        # ToolResult message appended
        from nexau.core.messages import Role

        assert len(agent.history) == 1
        msg = agent.history[0]
        assert msg.role == Role.TOOL
        assert msg.content[0].is_error is True
        assert "Permission denied" in str(msg.content[0].content)

        # Per-entry persist + final clear = 2 calls
        assert sm.update_pending_tool_calls.call_count == 2

        # First call: persist consumed flag
        first_call = sm.update_pending_tool_calls.call_args_list[0]
        assert first_call[1]["pending_tool_calls"]["tc_d"]["consumed"] is True

        # Second call: clear pending
        second_call = sm.update_pending_tool_calls.call_args_list[1]
        assert second_call[1]["pending_tool_calls"] is None

        # Entry marked consumed
        assert pending["tc_d"]["consumed"] is True

    @pytest.mark.anyio
    async def test_allow_reexecutes_tool(self):
        """Allowed tool_call re-executes the tool and writes ToolResult."""
        from nexau.archs.main_sub.agent import Agent

        tool = _make_tool("write_file", impl=lambda **kw: {"written": True})
        registry = ToolRegistry()
        registry.add_source("test", [tool])

        sm = _make_mock_session_manager()

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"
            agent._history = HistoryList([])
            agent.executor = MagicMock()
            agent.executor.tool_registry = registry

        pending = {
            "tc_a": _make_pending_entry("write_file", decision="allow", permission_key="path:/tmp"),
        }

        agent_state = _make_agent_state()
        ctx = _make_framework_context(tool_registry=registry)

        await agent._resume_pending_tool_calls(pending, agent_state, ctx)

        from nexau.core.messages import Role

        assert len(agent.history) == 1
        msg = agent.history[0]
        assert msg.role == Role.TOOL
        assert msg.content[0].is_error is False
        assert pending["tc_a"]["consumed"] is True

    @pytest.mark.anyio
    async def test_allow_once_reexecutes_with_temp_rules(self):
        """allow_once creates per-tool-call context with the permission_key in allow_rules."""
        from nexau.archs.main_sub.agent import Agent

        captured_ctx: list[FrameworkContext] = []

        def capturing_impl(ctx: Any = None, **kwargs: Any) -> dict[str, Any]:
            if ctx is not None:
                captured_ctx.append(ctx)
            return {"ok": True}

        tool = _make_tool("write_file", impl=capturing_impl)
        registry = ToolRegistry()
        registry.add_source("test", [tool])

        sm = _make_mock_session_manager()

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"
            agent._history = HistoryList([])
            agent.executor = MagicMock()
            agent.executor.tool_registry = registry

        pending = {
            "tc_ao": _make_pending_entry("write_file", decision="allow_once", permission_key="path:/var/log"),
        }

        agent_state = _make_agent_state()
        ctx = _make_framework_context(tool_registry=registry)

        await agent._resume_pending_tool_calls(pending, agent_state, ctx)

        assert len(captured_ctx) == 1
        assert captured_ctx[0].allow_rules == ["path:/var/log"]
        assert captured_ctx[0].deny_rules == []
        assert len(agent.history) == 1
        assert agent.history[0].content[0].is_error is False

    @pytest.mark.anyio
    async def test_mixed_allow_deny(self):
        """Mixed allow/deny decisions produce correct results for each."""
        from nexau.archs.main_sub.agent import Agent

        tool = _make_tool("my_tool", impl=lambda **kw: {"result": "ok"})
        registry = ToolRegistry()
        registry.add_source("test", [tool])

        sm = _make_mock_session_manager()

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"
            agent._history = HistoryList([])
            agent.executor = MagicMock()
            agent.executor.tool_registry = registry

        pending = {
            "tc_allow": _make_pending_entry("my_tool", decision="allow"),
            "tc_deny": _make_pending_entry("my_tool", decision="deny"),
        }

        agent_state = _make_agent_state()
        ctx = _make_framework_context(tool_registry=registry)

        await agent._resume_pending_tool_calls(pending, agent_state, ctx)

        assert len(agent.history) == 2
        results = {msg.content[0].tool_use_id: msg.content[0].is_error for msg in agent.history}
        assert results["tc_allow"] is False
        assert results["tc_deny"] is True

    @pytest.mark.anyio
    async def test_consumed_entries_skipped(self):
        """Already-consumed entries are skipped during resume (idempotency)."""
        from nexau.archs.main_sub.agent import Agent

        sm = _make_mock_session_manager()

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"
            agent._history = HistoryList([])
            agent.executor = MagicMock()
            agent.executor.tool_registry = ToolRegistry()

        pending = {
            "tc_done": _make_pending_entry("t", decision="allow"),
        }
        pending["tc_done"]["consumed"] = True

        agent_state = _make_agent_state()
        ctx = _make_framework_context()

        await agent._resume_pending_tool_calls(pending, agent_state, ctx)

        assert len(agent.history) == 0

        sm.update_pending_tool_calls.assert_called_once_with(
            user_id="u1",
            session_id="s1",
            pending_tool_calls=None,
        )

    @pytest.mark.anyio
    async def test_tool_not_found_during_resume(self):
        """Missing tool during resume produces error ToolResult."""
        from nexau.archs.main_sub.agent import Agent

        sm = _make_mock_session_manager()

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"
            agent._history = HistoryList([])
            agent.executor = MagicMock()
            agent.executor.tool_registry = ToolRegistry()

        pending = {
            "tc_missing": _make_pending_entry("ghost_tool", decision="allow"),
        }

        agent_state = _make_agent_state()
        ctx = _make_framework_context()

        await agent._resume_pending_tool_calls(pending, agent_state, ctx)

        assert len(agent.history) == 1
        assert agent.history[0].content[0].is_error is True
        assert "not found" in str(agent.history[0].content[0].content)

    @pytest.mark.anyio
    async def test_consumed_persisted_per_entry_for_crash_recovery(self):
        """Each entry's consumed flag is persisted to DB immediately after execution.

        RFC-0019: 崩溃恢复 — 每条 tool 执行后立即持久化 consumed 状态
        """
        import copy

        from nexau.archs.main_sub.agent import Agent

        tool_a = _make_tool("tool_a", impl=lambda **kw: {"a": 1})
        tool_b = _make_tool("tool_b", impl=lambda **kw: {"b": 2})
        registry = ToolRegistry()
        registry.add_source("test", [tool_a, tool_b])

        sm = _make_mock_session_manager()

        # Capture deep copies of each call's pending_tool_calls arg
        snapshots: list[dict[str, Any] | None] = []

        async def capture_update(**kwargs: Any) -> None:
            ptc = kwargs.get("pending_tool_calls")
            snapshots.append(copy.deepcopy(ptc))

        sm.update_pending_tool_calls = AsyncMock(side_effect=capture_update)

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"
            agent._history = HistoryList([])
            agent.executor = MagicMock()
            agent.executor.tool_registry = registry

        pending = {
            "tc_1": _make_pending_entry("tool_a", decision="allow"),
            "tc_2": _make_pending_entry("tool_b", decision="deny"),
        }

        agent_state = _make_agent_state()
        ctx = _make_framework_context(tool_registry=registry)

        await agent._resume_pending_tool_calls(pending, agent_state, ctx)

        # 2 per-entry persists + 1 final clear = 3 calls
        assert len(snapshots) == 3

        # First snapshot: tc_1 consumed, tc_2 not yet
        assert snapshots[0]["tc_1"]["consumed"] is True
        assert snapshots[0]["tc_2"].get("consumed") is not True

        # Second snapshot: both consumed
        assert snapshots[1]["tc_1"]["consumed"] is True
        assert snapshots[1]["tc_2"]["consumed"] is True

        # Third snapshot: final clear
        assert snapshots[2] is None

    @pytest.mark.anyio
    async def test_tool_exception_during_resume(self):
        """Tool that raises during resume captures error in ToolResult content.

        Tool.execute() catches regular exceptions internally and returns
        an error dict. The resume code sees this as a successful execution
        (no re-raised exception), but the error info is in the content.
        """
        from nexau.archs.main_sub.agent import Agent

        def failing_impl(**kwargs: Any) -> None:
            raise RuntimeError("kaboom")

        tool = _make_tool("boom_tool", impl=failing_impl)
        registry = ToolRegistry()
        registry.add_source("test", [tool])

        sm = _make_mock_session_manager()

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"
            agent._history = HistoryList([])
            agent.executor = MagicMock()
            agent.executor.tool_registry = registry

        pending = {
            "tc_boom": _make_pending_entry("boom_tool", decision="allow"),
        }

        agent_state = _make_agent_state()
        ctx = _make_framework_context(tool_registry=registry)

        await agent._resume_pending_tool_calls(pending, agent_state, ctx)

        from nexau.core.messages import Role

        assert len(agent.history) == 1
        assert agent.history[0].role == Role.TOOL
        # Tool.execute catches the exception and returns error dict in content
        assert "kaboom" in str(agent.history[0].content[0].content)
        assert pending["tc_boom"]["consumed"] is True


# ---------------------------------------------------------------------------
# Test hard-block in _run_async_inner
# ---------------------------------------------------------------------------


class TestHardBlock:
    """Test that _run_async_inner raises PendingPermissionsError."""

    @pytest.mark.anyio
    async def test_raises_with_unresolved_pending(self):
        """_run_async_inner raises PendingPermissionsError when unresolved decisions exist."""
        from nexau.archs.main_sub.agent import Agent

        unresolved_pending = {
            "tc_1": _make_pending_entry("write_file", decision=None),
            "tc_2": _make_pending_entry("run_shell", decision="allow"),
        }
        sm = _make_mock_session_manager(pending=unresolved_pending)

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"
            agent._run_complete = MagicMock()
            agent.executor = MagicMock()
            agent.executor.llm_caller = MagicMock()
            agent.executor.llm_caller.async_openai_client = MagicMock()

        with pytest.raises(PendingPermissionsError) as exc_info:
            await agent._run_async_inner(
                message="hello",
                run_id="run_test",
            )

        assert exc_info.value.session_id == "s1"
        assert "tc_1" in exc_info.value.pending

    @pytest.mark.anyio
    async def test_no_block_when_no_pending(self):
        """_run_async_inner does NOT raise when pending_tool_calls is None."""
        from nexau.archs.main_sub.agent import Agent

        sm = _make_mock_session_manager(pending=None)

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"
            agent._run_complete = MagicMock()
            agent.executor = MagicMock()
            agent.executor.llm_caller = MagicMock()
            agent.executor.llm_caller.async_openai_client = MagicMock()

        # Should proceed past the hard-block check (and fail on something else)
        with pytest.raises(Exception) as exc_info:
            await agent._run_async_inner(
                message="hello",
                run_id="run_test",
            )
        # If it raised PendingPermissionsError we'd fail; any other error means it passed the check
        assert not isinstance(exc_info.value, PendingPermissionsError)

    @pytest.mark.anyio
    async def test_no_block_when_all_resolved(self):
        """_run_async_inner does NOT raise when all pending decisions are resolved."""
        from nexau.archs.main_sub.agent import Agent

        all_resolved = {
            "tc_1": _make_pending_entry("t1", decision="allow"),
            "tc_2": _make_pending_entry("t2", decision="deny"),
        }
        sm = _make_mock_session_manager(pending=all_resolved)

        with patch.object(Agent, "__init__", lambda self, **kw: None):
            agent = Agent.__new__(Agent)
            agent._session_manager = sm
            agent._user_id = "u1"
            agent._session_id = "s1"
            agent._run_complete = MagicMock()
            agent.executor = MagicMock()
            agent.executor.llm_caller = MagicMock()
            agent.executor.llm_caller.async_openai_client = MagicMock()

        # Should proceed past the hard-block check (resume triggers, then fails elsewhere)
        with pytest.raises(Exception) as exc_info:
            await agent._run_async_inner(
                message="hello",
                run_id="run_test",
            )
        assert not isinstance(exc_info.value, PendingPermissionsError)
