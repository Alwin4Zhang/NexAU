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

"""Stop-signal propagation from parent executor to running sub-agents.

NAC 数据局生产 bug:
- 用户点停止后,sub-agent 仍跑到结束才停;现象 "运行中、工具调用中都停止不了对话"
- 根因: 子 Executor 是独立实例,自己的 stop_signal / _shutdown_event 与父隔离
- 修复: Executor.force_stop() 现在向下递归传播到 subagent_manager.running_sub_agents
  里所有 sub-agents 的 executor;Agent._interrupt() 也改用 force_stop() 复用传播
"""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.main_sub.execution.executor import Executor
from nexau.archs.main_sub.execution.stop_reason import AgentStopReason
from nexau.archs.main_sub.execution.subagent_manager import SubAgentManager
from nexau.archs.main_sub.history_list import HistoryList
from nexau.archs.tool.tool_registry import ToolRegistry


def _make_executor(agent_id: str = "test") -> Executor:
    """Build a lightweight executor for propagation tests."""
    return Executor(
        agent_name=agent_id,
        agent_id=agent_id,
        tool_registry=ToolRegistry(),
        sub_agents={},
        stop_tools=set(),
        openai_client=Mock(),
        llm_config=LLMConfig(model="gpt-4o"),
    )


def _mock_agent(agent_id: str, executor: Executor) -> Agent:
    """Create a typed Agent-shaped mock with the executor used by force_stop."""
    return cast(Agent, Mock(agent_id=agent_id, executor=executor))


class _FailingForceStopExecutor(Executor):
    """Executor whose recursive propagation raises, used to test best-effort traversal."""

    def __init__(self, agent_id: str = "bad") -> None:
        self.call_count = 0
        super().__init__(
            agent_name=agent_id,
            agent_id=agent_id,
            tool_registry=ToolRegistry(),
            sub_agents={},
            stop_tools=set(),
            openai_client=Mock(),
            llm_config=LLMConfig(model="gpt-4o"),
        )

    def _force_stop(self, visited_executor_ids: set[int]) -> None:
        del visited_executor_ids
        self.call_count += 1
        raise RuntimeError("boom")


class _ConcurrentPopForceStopExecutor(Executor):
    """Executor whose propagation mutates the parent running_sub_agents dict."""

    def __init__(self, agent_id: str, parent_manager: SubAgentManager) -> None:
        self.parent_manager = parent_manager
        self.call_count = 0
        super().__init__(
            agent_name=agent_id,
            agent_id=agent_id,
            tool_registry=ToolRegistry(),
            sub_agents={},
            stop_tools=set(),
            openai_client=Mock(),
            llm_config=LLMConfig(model="gpt-4o"),
        )

    def _force_stop(self, visited_executor_ids: set[int]) -> None:
        del visited_executor_ids
        self.call_count += 1
        self.parent_manager.running_sub_agents.pop("a", None)
        self.parent_manager.running_sub_agents.pop("b", None)


class _InterruptExecutorForTest(Executor):
    """Executor that records cleanup calls for Agent._interrupt(force=True)."""

    def __init__(self, cleanup_calls: list[str]) -> None:
        self.cleanup_calls = cleanup_calls
        super().__init__(
            agent_name="parent",
            agent_id="parent",
            tool_registry=ToolRegistry(),
            sub_agents={},
            stop_tools=set(),
            openai_client=Mock(),
            llm_config=LLMConfig(model="gpt-4o"),
        )

    def cleanup(self) -> None:
        self.cleanup_calls.append("cleanup")


class _RunComplete:
    """Minimal _run_complete waitable used by Agent._interrupt tests."""

    def __init__(self) -> None:
        self.wait = AsyncMock()


class _InterruptAgentForTest:
    """Agent-shaped helper that exercises _interrupt without constructing real runtime deps."""

    def __init__(self, executor: Executor) -> None:
        self.executor = executor
        self.config = AgentConfig(name="test", llm_config=LLMConfig(model="gpt-4o"))
        self._wait_for_execution_complete_calls: list[float] = []
        self._run_complete = _RunComplete()
        self.history = HistoryList([])
        self._last_context: dict[str, object] = {}
        self._persist_session_state_calls: list[dict[str, object]] = []
        self._close_async_llm_client_calls = 0

    async def _wait_for_execution_complete(self, *, timeout: float = 30.0) -> None:
        self._wait_for_execution_complete_calls.append(timeout)

    async def _persist_session_state(self, context: dict[str, object]) -> None:
        self._persist_session_state_calls.append(context)

    async def _close_async_llm_client(self) -> None:
        self._close_async_llm_client_calls += 1


class _AsyncSubAgentForTest:
    """Async sub-agent used to keep SubAgentManager.call_sub_agent_async running."""

    def __init__(
        self,
        executor: Executor,
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        self.agent_id = "async_child"
        self.agent_name = "async_child"
        self.executor = executor
        self._started = started
        self._release = release

    async def run_async(
        self,
        *,
        message: str,
        context: dict[str, object] | None,
        parent_agent_state: object | None,
        custom_llm_client_provider: object | None,
        trace_id: str | None,
    ) -> str:
        del message, context, parent_agent_state, custom_llm_client_provider, trace_id
        self._started.set()
        await self._release.wait()
        return "async child result"


class TestForceStopPropagation:
    def test_self_signals_set(self) -> None:
        """force_stop 必须 set 本 executor 的三个信号(原有行为不回归)。"""
        parent = _make_executor()

        parent.force_stop()

        assert parent.stop_signal is True
        assert parent._shutdown_event.is_set()
        assert parent._message_available.is_set()

    def test_propagates_to_single_sub_agent(self) -> None:
        """父 force_stop 必须把信号下发到 running sub-agent.executor。"""
        parent = _make_executor("parent")
        child_executor = _make_executor("child")
        parent.subagent_manager.running_sub_agents["child"] = _mock_agent("child", child_executor)

        parent.force_stop()

        assert child_executor.stop_signal is True
        assert child_executor._shutdown_event.is_set()
        assert child_executor._message_available.is_set()

    def test_propagates_to_multiple_sub_agents(self) -> None:
        """多个并行 sub-agent 同时被通知。"""
        parent = _make_executor("parent")
        children = [_make_executor(f"c{i}") for i in range(3)]
        for i, child_executor in enumerate(children):
            parent.subagent_manager.running_sub_agents[f"c{i}"] = _mock_agent(f"c{i}", child_executor)

        parent.force_stop()

        for child_executor in children:
            assert child_executor.stop_signal is True
            assert child_executor._shutdown_event.is_set()
            assert child_executor._message_available.is_set()

    def test_no_sub_agents_does_not_error(self) -> None:
        """无 running sub-agent 时不应抛错。"""
        parent = _make_executor()
        parent.force_stop()
        assert parent.stop_signal is True

    def test_one_sub_agent_failing_does_not_block_others(self) -> None:
        """一个 sub-agent 传播抛错不能阻断后续 sub-agent 接收信号。"""
        parent = _make_executor("parent")
        good_exec_a = _make_executor("a")
        bad_exec = _FailingForceStopExecutor("bad")
        good_exec_b = _make_executor("b")

        parent.subagent_manager.running_sub_agents["a"] = _mock_agent("a", good_exec_a)
        parent.subagent_manager.running_sub_agents["bad"] = _mock_agent("bad", bad_exec)
        parent.subagent_manager.running_sub_agents["b"] = _mock_agent("b", good_exec_b)

        parent.force_stop()

        assert good_exec_a.stop_signal is True
        assert bad_exec.call_count == 1
        assert good_exec_b.stop_signal is True

    def test_snapshot_isolates_concurrent_pop(self) -> None:
        """递归调用过程中 dict 被并发修改不能导致 iteration 失败。

        模拟一个真实场景: sub-agent A 的 force_stop 触发其 finally 路径回调,
        在同一线程内把自己从父 running_sub_agents 里 pop。Executor.force_stop
        先取 list(...) 快照,应当不受影响。
        """
        parent = _make_executor("parent")
        evil_exec = _ConcurrentPopForceStopExecutor("a", parent.subagent_manager)
        other_exec = _make_executor("b")

        parent.subagent_manager.running_sub_agents["a"] = _mock_agent("a", evil_exec)
        parent.subagent_manager.running_sub_agents["b"] = _mock_agent("b", other_exec)

        parent.force_stop()

        assert evil_exec.call_count == 1
        assert other_exec.stop_signal is True

    def test_cycle_does_not_recurse_forever(self) -> None:
        """异常 executor graph 成环时,visited set 应阻止递归爆栈。"""
        parent = _make_executor("parent")
        parent.subagent_manager.running_sub_agents["child"] = _mock_agent("child", parent)

        parent.force_stop()

        assert parent.stop_signal is True

    def test_interrupt_uses_force_stop_and_propagates_to_sub_agents(self) -> None:
        """Agent._interrupt 必须复用 force_stop 的递归传播路径。"""
        parent = _make_executor("parent")
        child_executor = _make_executor("child")
        parent.subagent_manager.running_sub_agents["child"] = _mock_agent("child", child_executor)
        agent = _InterruptAgentForTest(parent)

        result = asyncio.run(Agent._interrupt(cast(Agent, agent), force=False, timeout=0.25))

        assert parent.stop_signal is True
        assert child_executor.stop_signal is True
        assert agent._wait_for_execution_complete_calls == [0.25]
        assert agent._persist_session_state_calls == [{}]
        assert agent._close_async_llm_client_calls == 1
        assert result.stop_reason == AgentStopReason.USER_INTERRUPTED

    def test_force_interrupt_still_runs_cleanup(self) -> None:
        """force=True 路径在 force_stop 后仍执行硬清理。"""
        cleanup_calls: list[str] = []
        parent = _InterruptExecutorForTest(cleanup_calls)
        agent = _InterruptAgentForTest(parent)

        asyncio.run(Agent._interrupt(cast(Agent, agent), force=True))

        assert parent.stop_signal is True
        assert cleanup_calls == ["cleanup"]
        assert agent._close_async_llm_client_calls == 1


class TestNestedPropagation:
    """父 → 子 → 孙 三层递归传播。"""

    def test_grandchild_signals_set(self) -> None:
        """三层嵌套时,深层子 executor 也必须被通知。"""
        parent = _make_executor("parent")
        child = _make_executor("child")
        grandchild = _make_executor("grandchild")
        child.subagent_manager.running_sub_agents["g"] = _mock_agent("g", grandchild)
        parent.subagent_manager.running_sub_agents["child"] = _mock_agent("child", child)

        parent.force_stop()

        assert parent.stop_signal is True
        assert child.stop_signal is True
        assert child._shutdown_event.is_set()
        assert grandchild.stop_signal is True
        assert grandchild._shutdown_event.is_set()


class TestAsyncSubAgentPath:
    """Async sub-agent 调用路径也能被父 stop 传播打断。"""

    def test_running_async_sub_agent_receives_force_stop(self) -> None:
        """父 executor.force_stop 会传播到 call_sub_agent_async 正在运行的子 executor。"""

        async def run_scenario() -> tuple[str, bool, SubAgentManager]:
            child_config = AgentConfig(name="child", llm_config=LLMConfig(model="gpt-4o"))
            manager = SubAgentManager(agent_name="parent", sub_agents={"child": child_config})
            parent_executor = _make_executor("parent")
            parent_executor.subagent_manager = manager
            child_executor = _make_executor("child")
            started = asyncio.Event()
            release = asyncio.Event()
            async_agent = _AsyncSubAgentForTest(child_executor, started, release)

            with patch("nexau.archs.main_sub.agent.Agent.create", new_callable=AsyncMock, return_value=async_agent) as create_mock:
                task = asyncio.create_task(manager.call_sub_agent_async("child", "hello"))
                try:
                    await asyncio.wait_for(started.wait(), timeout=1.0)
                    assert "async_child" in manager.running_sub_agents

                    parent_executor.force_stop()

                    assert child_executor.stop_signal is True
                    assert child_executor._shutdown_event.is_set()
                    release.set()
                    result = await asyncio.wait_for(task, timeout=1.0)
                    return result, create_mock.awaited, manager
                except Exception:
                    release.set()
                    task.cancel()
                    raise

        result, create_awaited, manager = asyncio.run(run_scenario())

        assert create_awaited
        assert result.startswith("[sub_agent_id: async_child]")
        assert "async_child" not in manager.running_sub_agents


@pytest.mark.parametrize("count", [1, 5, 16])
def test_force_stop_scales_linearly(count: int) -> None:
    """parametrized: 不同数量 sub-agent 都能逐一收到信号。"""
    parent = _make_executor()
    executors = [_make_executor(f"s{i}") for i in range(count)]
    for i, executor in enumerate(executors):
        parent.subagent_manager.running_sub_agents[f"s{i}"] = _mock_agent(f"s{i}", executor)

    parent.force_stop()

    for executor in executors:
        assert executor.stop_signal is True
        assert executor._shutdown_event.is_set()
        assert executor._message_available.is_set()


# ---------------------------------------------------------------------------
# _await_with_shutdown_race: 主 agent 等待长 tool 时, shutdown 触发立即 cancel
# ---------------------------------------------------------------------------


class TestAwaitWithShutdownRace:
    """覆盖 Executor._await_with_shutdown_race 的所有边界。

    与 force_stop 配合达成"放弃 tool 结果立刻停"语义:
      shutdown 触发 → cancel 当前 tool future → 主循环边界查 stop_signal 退出。
    """

    @pytest.mark.anyio
    async def test_returns_result_when_no_shutdown(self) -> None:
        """正常 await 完成时返回 (result, False), 不触发 cancel。"""
        executor = _make_executor("e")

        async def work() -> str:
            await asyncio.sleep(0.01)
            return "ok"

        result, interrupted = await executor._await_with_shutdown_race(work())
        assert result == "ok"
        assert interrupted is False

    @pytest.mark.anyio
    async def test_cancels_within_one_poll_when_shutdown_already_set(self) -> None:
        """进入时 shutdown_event 已 set: 给 awaitable 一轮 poll 机会再 race cancel。

        不在入口立即 cancel: awaitable (如 _run_tool) 自己可能需要在 shutdown 时
        触发 emit 副作用, 提前 cancel 会跳过 emit。给 ≥1 轮 poll 让它执行。
        """
        executor = _make_executor("e")
        executor._shutdown_event.set()

        async def slow() -> str:
            await asyncio.sleep(10)
            return "should-not-return"

        result, interrupted = await executor._await_with_shutdown_race(slow(), poll_interval=0.02)
        assert result is None
        assert interrupted is True

    @pytest.mark.anyio
    async def test_awaitable_handles_shutdown_itself_returns_normally(self) -> None:
        """awaitable 自己看到 shutdown 走 emit + 返回 error tuple, helper 视为正常完成。

        对齐 _run_tool 内部: shutdown 时调 _emit_tool_error_result 后 return 一个
        ('tool', tc, (name, 'Shutdown in progress', True)) 错误 tuple, 不该被
        race cancel 替换成 (None, True)。
        """
        executor = _make_executor("e")
        executor._shutdown_event.set()

        async def runs_then_returns_error() -> tuple[str, str]:
            # 模拟 _run_tool 入口: 看到 shutdown, 走 emit, 返回 error
            assert executor._shutdown_event.is_set()
            return ("tool", "Shutdown in progress")

        result, interrupted = await executor._await_with_shutdown_race(runs_then_returns_error())
        assert result == ("tool", "Shutdown in progress")
        assert interrupted is False

    @pytest.mark.anyio
    async def test_cancels_when_shutdown_fires_midway(self) -> None:
        """await 中途 shutdown 触发 → 一轮 poll 内返回 (None, True)。"""
        executor = _make_executor("e")

        async def slow() -> str:
            await asyncio.sleep(5.0)
            return "should-not-return"

        # 100ms 后从另一 task 触发 shutdown
        async def trigger() -> None:
            await asyncio.sleep(0.1)
            executor._shutdown_event.set()

        trigger_task = asyncio.create_task(trigger())
        start = asyncio.get_event_loop().time()
        result, interrupted = await executor._await_with_shutdown_race(slow(), poll_interval=0.02)
        elapsed = asyncio.get_event_loop().time() - start
        await trigger_task

        assert result is None
        assert interrupted is True
        # 容忍 100ms 的 trigger 延迟 + 一次 poll (20ms) + CI 高负载下的调度抖动。
        # 阈值放宽到 1s 是为了避免 oversubscribed runner 上偶发 flake; 真正回归
        # (race 不工作) 会一路撞 awaitable 的 sleep(5.0), 仍能被这个阈值抓到。
        assert elapsed < 1.0, f"shutdown race took too long: {elapsed:.3f}s"

    @pytest.mark.anyio
    async def test_propagates_exception_from_awaitable(self) -> None:
        """awaitable 自己抛错时, 该错误从 helper 透传出来, 不被吞。"""
        executor = _make_executor("e")

        async def boom() -> None:
            raise RuntimeError("from inside tool")

        with pytest.raises(RuntimeError, match="from inside tool"):
            await executor._await_with_shutdown_race(boom())

    @pytest.mark.anyio
    async def test_cancelled_task_does_not_leak_warning(self, recwarn: pytest.WarningsRecorder) -> None:
        """task.cancel() 后必须 await task 消费 CancelledError, 否则 asyncio 抛
        'Task was destroyed but it is pending' / 'exception was never retrieved'
        类型的 RuntimeWarning, 在生产里堆栈刷屏。
        """
        executor = _make_executor("e")

        async def slow() -> str:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # 模拟一个不立即响应 cancel 的 awaitable
                await asyncio.sleep(0)
                raise
            return "should-not-return"

        async def trigger() -> None:
            await asyncio.sleep(0.05)
            executor._shutdown_event.set()

        trigger_task = asyncio.create_task(trigger())
        result, interrupted = await executor._await_with_shutdown_race(slow(), poll_interval=0.02)
        await trigger_task

        # 给 asyncio 一点时间触发 destruction warning (如果有)
        await asyncio.sleep(0.05)

        assert result is None
        assert interrupted is True
        # 不应有 task destruction / never-retrieved 类的 warning
        leaked = [w for w in recwarn.list if "Task was destroyed" in str(w.message) or "never retrieved" in str(w.message)]
        assert not leaked, f"unexpected warnings leaked: {[str(w.message) for w in leaked]}"

    @pytest.mark.anyio
    async def test_fast_path_when_awaitable_already_done(self) -> None:
        """awaitable 比第一次 poll 还快完成时也能正确返回结果。"""
        executor = _make_executor("e")

        async def instant() -> int:
            return 42

        result, interrupted = await executor._await_with_shutdown_race(instant(), poll_interval=0.1)
        assert result == 42
        assert interrupted is False
