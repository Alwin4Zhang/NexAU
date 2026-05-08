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

"""T3 executor integration tests for RFC-0019 permission handling.

RFC-0019: Executor 权限集成测试

Covers:
- Tool with allow_rules=["**"] → normal execution
- Tool that raises PermissionDenied → DenyOutcome → denial ToolResult
- Tool that raises AskPermission → AskOutcome → pending_tool_calls written
- Parallel: one Ask + one Allow → mixed outcomes
- Parallel: multiple Asks → all written to pending_tool_calls
- Permission cache built from Tool.permissions
- Per-tool-call FrameworkContext construction
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock, patch

import pytest

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.agent_context import AgentContext, GlobalStorage
from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.main_sub.execution.executor import Executor
from nexau.archs.main_sub.execution.parse_structures import ParsedResponse, ToolCall
from nexau.archs.main_sub.framework_context import FrameworkContext
from nexau.archs.permissions.types import (
    AskOutcome,
    AskPermission,
    DenyOutcome,
    PermissionDenied,
)
from nexau.archs.tool.tool import Tool
from nexau.archs.tool.tool_registry import ToolRegistry

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_registry(tools: list[Tool] | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    if tools:
        registry.add_source("test", tools)
    return registry


def _make_config() -> LLMConfig:
    return LLMConfig(
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        api_type="openai_chat_completion",
    )


def _make_executor(
    *,
    tools: list[Tool] | None = None,
    session_manager: Mock | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
) -> Executor:
    return Executor(
        agent_name="perm_test_agent",
        agent_id="perm_test_id",
        tool_registry=_make_tool_registry(tools),
        sub_agents={},
        stop_tools=set(),
        openai_client=Mock(),
        llm_config=_make_config(),
        team_mode=False,
        session_manager=session_manager,
        user_id=user_id,
        session_id=session_id,
    )


def _make_agent_state() -> AgentState:
    return AgentState(
        agent_name="perm_test_agent",
        agent_id="perm_test_id",
        run_id="run_perm",
        root_run_id="run_perm",
        context=AgentContext({}),
        global_storage=GlobalStorage(),
        tool_registry=ToolRegistry(),
    )


def _make_framework_context() -> FrameworkContext:
    return FrameworkContext(
        agent_name="perm_test_agent",
        agent_id="perm_test_id",
        run_id="run_perm",
        root_run_id="run_perm",
        _tool_registry=ToolRegistry(),
        _shutdown_event=threading.Event(),
    )


def _make_tool_call(
    tool_name: str = "test_tool",
    tool_call_id: str = "tc_1",
) -> ToolCall:
    return ToolCall(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        parameters={"x": "1"},
        source="structured",
    )


def _default_impl(**kwargs: object) -> dict[str, bool]:
    return {"ok": True}


def _make_tool(
    name: str,
    impl: Callable[..., Any] | None = None,
    permissions: dict[str, list[str]] | None = None,
) -> Tool:
    """Create a test Tool with an optional implementation and permissions."""
    if impl is None:
        impl = _default_impl
    return Tool(
        name=name,
        description=f"Test tool: {name}",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        implementation=impl,
        permissions=permissions,
    )


# ---------------------------------------------------------------------------
# Permission cache tests
# ---------------------------------------------------------------------------


class TestBuildPermissionCacheFromTools:
    """_build_permission_cache_from_tools builds cache from Tool.permissions."""

    def test_empty_registry(self):
        executor = _make_executor(tools=None)
        cache = executor._build_permission_cache_from_tools()
        assert cache == {}

    def test_tool_without_permissions(self):
        tool = _make_tool("plain_tool", permissions=None)
        executor = _make_executor(tools=[tool])
        cache = executor._build_permission_cache_from_tools()
        assert "plain_tool" not in cache

    def test_tool_with_permissions(self):
        tool = _make_tool(
            "guarded_tool",
            permissions={"allow": ["*.py"], "deny": ["secrets/*"]},
        )
        executor = _make_executor(tools=[tool])
        cache = executor._build_permission_cache_from_tools()
        assert cache["guarded_tool"] == (["*.py"], ["secrets/*"])

    def test_multiple_tools_mixed(self):
        tool_a = _make_tool("tool_a", permissions={"allow": ["a"], "deny": []})
        tool_b = _make_tool("tool_b", permissions=None)
        tool_c = _make_tool("tool_c", permissions={"allow": [], "deny": ["d"]})
        executor = _make_executor(tools=[tool_a, tool_b, tool_c])
        cache = executor._build_permission_cache_from_tools()
        assert "tool_a" in cache
        assert "tool_b" not in cache
        assert "tool_c" in cache


# ---------------------------------------------------------------------------
# _execute_tool_call_safe permission handling
# ---------------------------------------------------------------------------


class TestExecuteToolCallSafePermission:
    """_execute_tool_call_safe catches AskPermission/PermissionDenied."""

    def test_allow_rules_wildcard_normal_execution(self):
        """Tool with allow_rules=["**"] → normal execution, no permission error."""
        tool = _make_tool("allowed_tool")
        executor = _make_executor(tools=[tool])
        agent_state = _make_agent_state()
        framework_ctx = _make_framework_context()

        tc = _make_tool_call(tool_name="allowed_tool", tool_call_id="tc_allow")
        permission_cache: dict[str, tuple[list[str], list[str]]] = {}

        with patch("nexau.archs.main_sub.agent_context.get_context", return_value=None):
            tool_name, result, is_error = executor._execute_tool_call_safe(
                tc,
                agent_state,
                framework_ctx,
                permission_cache,
            )

        assert tool_name == "allowed_tool"
        assert is_error is False
        assert not isinstance(result, (AskOutcome, DenyOutcome))

    def test_permission_denied_returns_deny_outcome(self):
        """Tool that raises PermissionDenied → DenyOutcome returned."""

        def denied_impl(**kwargs: object) -> None:
            raise PermissionDenied(reason="path /etc/passwd is blocked", permission_key="path:/etc/passwd")

        tool = _make_tool("write_file", impl=denied_impl)
        executor = _make_executor(tools=[tool])
        agent_state = _make_agent_state()
        framework_ctx = _make_framework_context()
        tc = _make_tool_call(tool_name="write_file", tool_call_id="tc_deny")

        with patch("nexau.archs.main_sub.agent_context.get_context", return_value=None):
            tool_name, result, is_error = executor._execute_tool_call_safe(
                tc,
                agent_state,
                framework_ctx,
                permission_cache={},
            )

        assert tool_name == "write_file"
        assert isinstance(result, DenyOutcome)
        assert result.reason == "path /etc/passwd is blocked"
        assert result.permission_key == "path:/etc/passwd"
        assert result.tool_call_id == "tc_deny"
        assert is_error is True

    def test_ask_permission_returns_ask_outcome(self):
        """Tool that raises AskPermission → AskOutcome returned."""

        def asking_impl(**kwargs: object) -> None:
            raise AskPermission(prompt="Allow writing to /tmp/data.json?", permission_key="path:/tmp/data.json")

        tool = _make_tool("write_file", impl=asking_impl)
        executor = _make_executor(tools=[tool])
        agent_state = _make_agent_state()
        framework_ctx = _make_framework_context()
        tc = _make_tool_call(tool_name="write_file", tool_call_id="tc_ask")

        with patch("nexau.archs.main_sub.agent_context.get_context", return_value=None):
            tool_name, result, is_error = executor._execute_tool_call_safe(
                tc,
                agent_state,
                framework_ctx,
                permission_cache={},
            )

        assert tool_name == "write_file"
        assert isinstance(result, AskOutcome)
        assert result.prompt == "Allow writing to /tmp/data.json?"
        assert result.permission_key == "path:/tmp/data.json"
        assert result.tool_call_id == "tc_ask"
        assert result.tool_name == "write_file"
        assert result.parameters == {"x": "1"}
        assert is_error is False

    def test_per_tool_call_context_constructed(self):
        """When permission_cache is provided, for_tool_call() is called with correct rules."""
        tool = _make_tool("guarded_tool")
        executor = _make_executor(tools=[tool])
        agent_state = _make_agent_state()
        framework_ctx = _make_framework_context()
        tc = _make_tool_call(tool_name="guarded_tool", tool_call_id="tc_ctx")

        permission_cache = {"guarded_tool": (["*.py"], ["secrets/*"])}

        with (
            patch.object(framework_ctx, "for_tool_call", wraps=framework_ctx.for_tool_call) as mock_ftc,
            patch("nexau.archs.main_sub.agent_context.get_context", return_value=None),
        ):
            executor._execute_tool_call_safe(
                tc,
                agent_state,
                framework_ctx,
                permission_cache,
            )

        mock_ftc.assert_called_once_with(
            tool_name="guarded_tool",
            allow_rules=["*.py"],
            deny_rules=["secrets/*"],
        )

    def test_no_permission_cache_skips_for_tool_call(self):
        """When permission_cache is None, for_tool_call() is NOT called."""
        tool = _make_tool("plain_tool")
        executor = _make_executor(tools=[tool])
        agent_state = _make_agent_state()
        framework_ctx = _make_framework_context()
        tc = _make_tool_call(tool_name="plain_tool", tool_call_id="tc_npc")

        with (
            patch.object(framework_ctx, "for_tool_call") as mock_ftc,
            patch("nexau.archs.main_sub.agent_context.get_context", return_value=None),
        ):
            executor._execute_tool_call_safe(
                tc,
                agent_state,
                framework_ctx,
                None,
            )

        mock_ftc.assert_not_called()

    def test_uncached_tool_defaults_to_wildcard_allow(self):
        """Tool not in cache gets default (["**"], []) → for_tool_call called with wildcards."""
        tool = _make_tool("uncached_tool")
        executor = _make_executor(tools=[tool])
        agent_state = _make_agent_state()
        framework_ctx = _make_framework_context()
        tc = _make_tool_call(tool_name="uncached_tool", tool_call_id="tc_uc")

        permission_cache: dict[str, tuple[list[str], list[str]]] = {"other_tool": (["x"], ["y"])}

        with (
            patch.object(framework_ctx, "for_tool_call", wraps=framework_ctx.for_tool_call) as mock_ftc,
            patch("nexau.archs.main_sub.agent_context.get_context", return_value=None),
        ):
            executor._execute_tool_call_safe(
                tc,
                agent_state,
                framework_ctx,
                permission_cache,
            )

        mock_ftc.assert_called_once_with(
            tool_name="uncached_tool",
            allow_rules=["**"],
            deny_rules=[],
        )


# ---------------------------------------------------------------------------
# _execute_parsed_calls permission handling (sync path)
# ---------------------------------------------------------------------------


class TestExecuteParsedCallsSyncPermission:
    """Sync _execute_parsed_calls handles AskOutcome and DenyOutcome correctly."""

    def test_deny_outcome_produces_error_feedback(self):
        """DenyOutcome → is_error=True feedback entry, no AskOutcome."""

        def denied_impl(**kwargs: object) -> None:
            raise PermissionDenied(reason="blocked", permission_key="key:blocked")

        tool = _make_tool("blocked_tool", impl=denied_impl)
        executor = _make_executor(tools=[tool])
        agent_state = _make_agent_state()
        framework_ctx = _make_framework_context()

        tc = _make_tool_call(tool_name="blocked_tool", tool_call_id="tc_bp")
        parsed = ParsedResponse(original_response="calling blocked_tool", tool_calls=[tc])

        with patch("nexau.archs.main_sub.agent_context.get_context", return_value=None):
            _, _, _, feedbacks, ask_outcomes = executor._execute_parsed_calls(
                parsed,
                agent_state,
                framework_context=framework_ctx,
                permission_cache={},
            )

        assert ask_outcomes == []
        assert len(feedbacks) == 1
        assert feedbacks[0]["is_error"] is True
        assert "Permission denied" in feedbacks[0]["content"]
        assert "blocked" in feedbacks[0]["content"]

    def test_ask_outcome_collected_not_in_feedbacks(self):
        """AskOutcome → appears in ask_outcomes, NOT in feedbacks."""

        def asking_impl(**kwargs: object) -> None:
            raise AskPermission(prompt="Allow?", permission_key="key:ask")

        tool = _make_tool("ask_tool", impl=asking_impl)
        executor = _make_executor(tools=[tool])
        agent_state = _make_agent_state()
        framework_ctx = _make_framework_context()

        tc = _make_tool_call(tool_name="ask_tool", tool_call_id="tc_ap")
        parsed = ParsedResponse(original_response="calling ask_tool", tool_calls=[tc])

        with patch("nexau.archs.main_sub.agent_context.get_context", return_value=None):
            _, _, _, feedbacks, ask_outcomes = executor._execute_parsed_calls(
                parsed,
                agent_state,
                framework_context=framework_ctx,
                permission_cache={},
            )

        assert len(ask_outcomes) == 1
        assert ask_outcomes[0].tool_name == "ask_tool"
        assert ask_outcomes[0].prompt == "Allow?"
        assert len(feedbacks) == 0

    def test_mixed_allow_and_ask(self):
        """Parallel: one normal tool + one Ask → mixed outcomes."""

        def asking_impl(**kwargs: object) -> None:
            raise AskPermission(prompt="Allow?", permission_key="key:ask")

        normal_tool = _make_tool("normal_tool")
        asking_tool = _make_tool("asking_tool", impl=asking_impl)
        executor = _make_executor(tools=[normal_tool, asking_tool])
        agent_state = _make_agent_state()
        framework_ctx = _make_framework_context()

        tc_normal = _make_tool_call(tool_name="normal_tool", tool_call_id="tc_n")
        tc_ask = _make_tool_call(tool_name="asking_tool", tool_call_id="tc_a")
        parsed = ParsedResponse(
            original_response="parallel calls",
            tool_calls=[tc_normal, tc_ask],
        )

        with patch("nexau.archs.main_sub.agent_context.get_context", return_value=None):
            _, _, _, feedbacks, ask_outcomes = executor._execute_parsed_calls(
                parsed,
                agent_state,
                framework_context=framework_ctx,
                permission_cache={},
            )

        assert len(ask_outcomes) == 1
        assert ask_outcomes[0].tool_name == "asking_tool"

        normal_feedbacks = [f for f in feedbacks if f.get("is_error") is not True]
        assert len(normal_feedbacks) == 1

    def test_multiple_asks_all_collected(self):
        """Parallel: multiple Asks → all collected in ask_outcomes."""

        def ask_impl_a(**kwargs: object) -> None:
            raise AskPermission(prompt="Allow A?", permission_key="key:a")

        def ask_impl_b(**kwargs: object) -> None:
            raise AskPermission(prompt="Allow B?", permission_key="key:b")

        tool_a = _make_tool("ask_a", impl=ask_impl_a)
        tool_b = _make_tool("ask_b", impl=ask_impl_b)
        executor = _make_executor(tools=[tool_a, tool_b])
        agent_state = _make_agent_state()
        framework_ctx = _make_framework_context()

        tc_a = _make_tool_call(tool_name="ask_a", tool_call_id="tc_ma")
        tc_b = _make_tool_call(tool_name="ask_b", tool_call_id="tc_mb")
        parsed = ParsedResponse(
            original_response="multi ask",
            tool_calls=[tc_a, tc_b],
        )

        with patch("nexau.archs.main_sub.agent_context.get_context", return_value=None):
            _, _, _, feedbacks, ask_outcomes = executor._execute_parsed_calls(
                parsed,
                agent_state,
                framework_context=framework_ctx,
                permission_cache={},
            )

        assert len(ask_outcomes) == 2
        assert len(feedbacks) == 0
        keys = {o.permission_key for o in ask_outcomes}
        assert keys == {"key:a", "key:b"}

    def test_no_permission_cache_normal_execution(self):
        """permission_cache=None → normal execution, no permission logic triggered."""
        tool = _make_tool("regular_tool")
        executor = _make_executor(tools=[tool])
        agent_state = _make_agent_state()
        framework_ctx = _make_framework_context()

        tc = _make_tool_call(tool_name="regular_tool", tool_call_id="tc_reg")
        parsed = ParsedResponse(original_response="regular call", tool_calls=[tc])

        with patch("nexau.archs.main_sub.agent_context.get_context", return_value=None):
            _, _, _, feedbacks, ask_outcomes = executor._execute_parsed_calls(
                parsed,
                agent_state,
                framework_context=framework_ctx,
                permission_cache=None,
            )

        assert ask_outcomes == []
        assert len(feedbacks) == 1
        assert feedbacks[0].get("is_error") is not True


# ---------------------------------------------------------------------------
# _execute_parsed_calls_async permission handling
# ---------------------------------------------------------------------------


class TestExecuteParsedCallsAsyncPermission:
    """Async _execute_parsed_calls_async handles AskOutcome and DenyOutcome."""

    @pytest.mark.anyio
    async def test_deny_outcome_async(self):
        """Async path: PermissionDenied → DenyOutcome → error feedback."""

        def denied_impl(**kwargs: object) -> None:
            raise PermissionDenied(reason="no write access", permission_key="path:/root")

        tool = _make_tool("async_blocked", impl=denied_impl)
        executor = _make_executor(tools=[tool])
        agent_state = _make_agent_state()
        framework_ctx = _make_framework_context()

        tc = _make_tool_call(tool_name="async_blocked", tool_call_id="tc_ad")
        parsed = ParsedResponse(original_response="async denied", tool_calls=[tc])

        with patch("nexau.archs.main_sub.agent_context.get_context", return_value=None):
            _, _, _, feedbacks, ask_outcomes = await executor._execute_parsed_calls_async(
                parsed,
                agent_state,
                framework_context=framework_ctx,
                permission_cache={},
            )

        assert ask_outcomes == []
        assert len(feedbacks) == 1
        assert feedbacks[0]["is_error"] is True
        assert "Permission denied" in feedbacks[0]["content"]

    @pytest.mark.anyio
    async def test_ask_outcome_async(self):
        """Async path: AskPermission → AskOutcome, no feedback."""

        def asking_impl(**kwargs: object) -> None:
            raise AskPermission(prompt="Allow shell: rm -rf?", permission_key="shell:rm")

        tool = _make_tool("async_ask", impl=asking_impl)
        executor = _make_executor(tools=[tool])
        agent_state = _make_agent_state()
        framework_ctx = _make_framework_context()

        tc = _make_tool_call(tool_name="async_ask", tool_call_id="tc_aa")
        parsed = ParsedResponse(original_response="async ask", tool_calls=[tc])

        with patch("nexau.archs.main_sub.agent_context.get_context", return_value=None):
            _, _, _, feedbacks, ask_outcomes = await executor._execute_parsed_calls_async(
                parsed,
                agent_state,
                framework_context=framework_ctx,
                permission_cache={},
            )

        assert len(ask_outcomes) == 1
        assert ask_outcomes[0].tool_name == "async_ask"
        assert ask_outcomes[0].prompt == "Allow shell: rm -rf?"
        assert len(feedbacks) == 0

    @pytest.mark.anyio
    async def test_mixed_outcomes_async(self):
        """Async path: parallel normal + Ask + Deny → correct mixed results."""

        def asking_impl(**kwargs: object) -> None:
            raise AskPermission(prompt="Allow?", permission_key="key:x")

        def denied_impl(**kwargs: object) -> None:
            raise PermissionDenied(reason="nope", permission_key="key:y")

        normal_tool = _make_tool("ok_tool")
        ask_tool = _make_tool("ask_tool", impl=asking_impl)
        deny_tool = _make_tool("deny_tool", impl=denied_impl)
        executor = _make_executor(tools=[normal_tool, ask_tool, deny_tool])
        agent_state = _make_agent_state()
        framework_ctx = _make_framework_context()

        tc1 = _make_tool_call(tool_name="ok_tool", tool_call_id="tc_ok")
        tc2 = _make_tool_call(tool_name="ask_tool", tool_call_id="tc_ask2")
        tc3 = _make_tool_call(tool_name="deny_tool", tool_call_id="tc_deny2")
        parsed = ParsedResponse(
            original_response="mixed parallel",
            tool_calls=[tc1, tc2, tc3],
        )

        with patch("nexau.archs.main_sub.agent_context.get_context", return_value=None):
            _, _, _, feedbacks, ask_outcomes = await executor._execute_parsed_calls_async(
                parsed,
                agent_state,
                framework_context=framework_ctx,
                permission_cache={},
            )

        assert len(ask_outcomes) == 1
        assert ask_outcomes[0].tool_name == "ask_tool"

        deny_feedbacks = [f for f in feedbacks if f["is_error"] is True and "Permission denied" in f["content"]]
        assert len(deny_feedbacks) == 1

        ok_feedbacks = [f for f in feedbacks if f.get("is_error") is not True]
        assert len(ok_feedbacks) == 1


# ---------------------------------------------------------------------------
# tool_executor propagation tests
# ---------------------------------------------------------------------------


class TestToolExecutorPermissionPropagation:
    """Verify tool_executor._execute_tool_inner lets permission exceptions propagate."""

    def test_ask_permission_propagates(self):
        """AskPermission raised by tool impl is NOT caught by _execute_tool_inner."""
        from nexau.archs.main_sub.execution.tool_executor import ToolExecutor

        def asking_impl(**kwargs: object) -> None:
            raise AskPermission(prompt="Allow?", permission_key="key:prop")

        tool = _make_tool("prop_tool", impl=asking_impl)
        registry = _make_tool_registry([tool])
        tool_executor = ToolExecutor(tool_registry=registry, stop_tools=set())
        agent_state = _make_agent_state()

        with pytest.raises(AskPermission) as exc_info:
            tool_executor._execute_tool_inner(
                agent_state=agent_state,
                sandbox=None,
                tool=tool,
                tool_name="prop_tool",
                tool_parameters={"x": "1"},
                tool_call_id="tc_prop",
            )

        assert exc_info.value.prompt == "Allow?"
        assert exc_info.value.permission_key == "key:prop"

    def test_permission_denied_propagates(self):
        """PermissionDenied raised by tool impl is NOT caught by _execute_tool_inner."""
        from nexau.archs.main_sub.execution.tool_executor import ToolExecutor

        def denied_impl(**kwargs: object) -> None:
            raise PermissionDenied(reason="nope", permission_key="key:deny_prop")

        tool = _make_tool("deny_prop_tool", impl=denied_impl)
        registry = _make_tool_registry([tool])
        tool_executor = ToolExecutor(tool_registry=registry, stop_tools=set())
        agent_state = _make_agent_state()

        with pytest.raises(PermissionDenied) as exc_info:
            tool_executor._execute_tool_inner(
                agent_state=agent_state,
                sandbox=None,
                tool=tool,
                tool_name="deny_prop_tool",
                tool_parameters={"x": "1"},
                tool_call_id="tc_dp",
            )

        assert exc_info.value.reason == "nope"
        assert exc_info.value.permission_key == "key:deny_prop"

    def test_regular_exception_still_caught(self):
        """Regular exceptions are still caught and returned as error results."""
        from nexau.archs.main_sub.execution.tool_executor import ToolExecutor

        def failing_impl(**kwargs: object) -> None:
            raise ValueError("something broke")

        tool = _make_tool("fail_tool", impl=failing_impl)
        registry = _make_tool_registry([tool])
        tool_executor = ToolExecutor(tool_registry=registry, stop_tools=set())
        agent_state = _make_agent_state()

        result = tool_executor._execute_tool_inner(
            agent_state=agent_state,
            sandbox=None,
            tool=tool,
            tool_name="fail_tool",
            tool_parameters={"x": "1"},
            tool_call_id="tc_fail",
        )

        assert result is not None
