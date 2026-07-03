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

"""End-to-end stop-propagation tests with real Agent + Executor.

PR #588 follow-up: 单元测试 (tests/unit/test_subagent_stop_propagation.py) 验证了
force_stop / _await_with_shutdown_race 的隔离行为, 但没覆盖真实
``_execute_parsed_calls_async`` 的 tool loop 中断路径。这里补端到端集成测试:

- 真实 ``Agent.create()`` 初始化 (不 mock Agent.create, 真走 executor 主循环)
- 仅 mock ``LLMCaller.call_llm_async`` 注入可控 ModelResponse 让 agent 跑出
  确定的 tool 序列
- 触发 ``executor.force_stop()`` 验证主 agent 退出时延 + 工具调用次数 +
  传播信号到 running sub-agents
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.main_sub.execution.model_response import ModelResponse, ModelToolCall
from nexau.archs.tool import Tool

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _make_agent(
    name: str,
    *,
    tools: list[Tool] | None = None,
    sub_agents: dict[str, AgentConfig] | None = None,
    max_iterations: int = 80,
) -> Agent:
    """Build a real Agent in-process with a stub OpenAI client.

    走 ``Agent.create()`` async factory 以便从 event loop 上下文中构造,
    与生产 transport (HTTP/SSE) 调用 ``Agent.create`` 的路径一致。
    """
    with patch("nexau.archs.main_sub.agent.openai") as mock_openai:
        mock_openai.OpenAI.return_value = Mock()
        config = AgentConfig(
            name=name,
            llm_config=LLMConfig(
                model="gpt-4o-mini",
                base_url="http://mock.local",
                api_key="sk-mock",
            ),
            tool_call_mode="openai",
            max_iterations=max_iterations,
            tools=tools or [],
            sub_agents=sub_agents or {},
        )
        return await Agent.create(config=config)


def _tool_call_response(name: str, args: dict[str, Any] | None = None) -> ModelResponse:
    return ModelResponse(
        content=None,
        tool_calls=[
            ModelToolCall(
                call_id=f"c_{name}_{id(args)}",
                name=name,
                arguments=args or {},
                raw_arguments="{}",
            )
        ],
    )


# ---------------------------------------------------------------------------
# Case A: parent 卡在 sync long_tool, force_stop 应让 helper race cancel + 主退出
# ---------------------------------------------------------------------------


class TestParentLongToolRaceCancel:
    @pytest.mark.anyio
    async def test_long_running_serial_tool_releases_main_on_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """父在 await sync long_tool 时触发 force_stop, 主 agent 应秒级退出。

        覆盖 ``_execute_parsed_calls_async`` 的 serial loop 路径:
          - tool 走 ``asyncio.to_thread(_execute_tool_call_safe, ...)`` 阻塞 5s
          - ``_await_with_shutdown_race`` 应在一次 poll (50ms) 内 cancel 主 future
          - 主循环边界查 stop_signal 退出, 总耗时 ≪ 5s tool 自然返回
        """
        tool_calls: list[float] = []

        def long_sync_tool() -> str:
            tool_calls.append(time.time())
            time.sleep(5.0)
            return "long done"

        long_tool = Tool(
            name="long_sync_tool",
            description="Sleep 5s synchronously.",
            input_schema={"type": "object", "properties": {}, "required": []},
            implementation=long_sync_tool,
            disable_parallel=True,
        )

        parent = await _make_agent("parent_long", tools=[long_tool], max_iterations=5)
        monkeypatch.setattr(
            parent.executor.llm_caller,
            "call_llm_async",
            AsyncMock(side_effect=lambda *a, **kw: _tool_call_response("long_sync_tool")),
        )

        t0 = time.time()
        run_task = asyncio.create_task(parent.run_async(message="please run long tool"))

        # 等 tool worker 真的开始跑
        await asyncio.sleep(0.5)
        assert len(tool_calls) == 1, "long_sync_tool 应该已经进 worker"

        parent.executor.force_stop()
        await asyncio.wait_for(run_task, timeout=8.0)
        elapsed = time.time() - t0

        # 关键断言: 主退出远早于 5s (tool 自然返回), 即 race helper 真的中断了 await
        assert elapsed < 2.0, f"主 agent 卡了 {elapsed:.2f}s, 期望 <2s"
        # tool worker 线程已经在跑 (Python 限制无法 cancel), 但只跑了 1 次
        assert len(tool_calls) == 1


# ---------------------------------------------------------------------------
# Case B: parent 调 sub-agent (跑在 to_thread worker), force_stop 应递归传播
# ---------------------------------------------------------------------------


class TestParentSubAgentChainStop:
    @pytest.mark.anyio
    async def test_running_sub_agent_receives_propagated_stop_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """父在调 sub-agent 时 force_stop, 子 executor 应收到 stop_signal。

        覆盖 ``Executor.force_stop`` 递归 + ``running_sub_agents`` 真实注册路径。
        不 mock Agent.create / SubAgentManager.call_sub_agent_async, 让子 agent
        通过 subagent_manager 真正注册到 parent.executor.subagent_manager.running_sub_agents。
        """
        # 1. 子 agent: 一个会自循环调 echo_tool 的 agent
        echo_call_count = {"n": 0}

        def echo_tool() -> str:
            echo_call_count["n"] += 1
            time.sleep(0.05)
            return f"tick {echo_call_count['n']}"

        echo = Tool(
            name="echo_tool",
            description="Tick.",
            input_schema={"type": "object", "properties": {}, "required": []},
            implementation=echo_tool,
            disable_parallel=True,
        )

        child = await _make_agent("child", tools=[echo], max_iterations=200)
        # 把 child 注册到 parent.running_sub_agents (模拟 sub-agent 正在 run)
        parent = await _make_agent("parent_chain")
        parent.executor.subagent_manager.running_sub_agents[child.agent_id] = child

        # 2. mock child LLM: 永远返回调 echo_tool 让 child 进循环
        monkeypatch.setattr(
            child.executor.llm_caller,
            "call_llm_async",
            AsyncMock(side_effect=lambda *a, **kw: _tool_call_response("echo_tool")),
        )

        # 3. 启子, 等它进循环
        child_task = asyncio.create_task(child.run_async(message="loop forever"))
        await asyncio.sleep(1.0)
        pre_stop_count = echo_call_count["n"]
        assert pre_stop_count > 3, f"child 应该已经跑了几轮 echo, 实测 {pre_stop_count}"
        assert child.executor.stop_signal is False, "stop 前 child stop_signal 应为 False"

        # 4. 父 force_stop -> 应递归 set 子的 stop_signal
        parent.executor.force_stop()

        # 5. 子应在 1 轮 iteration 内退出
        await asyncio.wait_for(child_task, timeout=5.0)

        assert child.executor.stop_signal is True
        assert child.executor._shutdown_event.is_set()
        # 触发 stop 后最多再多跑几次 (循环已进 to_thread, 等当次返回才 check stop_signal)
        post_stop_count = echo_call_count["n"]
        extra_iters = post_stop_count - pre_stop_count
        assert extra_iters <= 3, f"触发 stop 后多跑了 {extra_iters} 次 echo, 期望 ≤3"


# ---------------------------------------------------------------------------
# Case C: parallel tool gather race cancel
# ---------------------------------------------------------------------------


class TestParallelGatherRaceCancel:
    @pytest.mark.anyio
    async def test_parallel_tools_release_main_on_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """3 个 parallel sync tool 同时跑, force_stop 应 cancel 整个 gather。

        覆盖 ``_execute_parsed_calls_async`` 的 parallel gather 路径。
        修复前: 主 agent 等满 max(tool durations); 修复后: 一轮 poll 内放弃。
        """
        tool_starts: list[float] = []

        def slow_parallel_tool() -> str:
            tool_starts.append(time.time())
            time.sleep(3.0)
            return "p done"

        # 注意: 不设 disable_parallel, 让多个 call 并行
        slow_p = Tool(
            name="slow_p",
            description="Slow parallel tool.",
            input_schema={"type": "object", "properties": {}, "required": []},
            implementation=slow_parallel_tool,
        )

        parent = await _make_agent("parent_parallel", tools=[slow_p], max_iterations=5)

        # mock LLM: 一次返回 3 个 parallel tool_calls
        def make_parallel_response(*_a: Any, **_kw: Any) -> ModelResponse:
            return ModelResponse(
                content=None,
                tool_calls=[ModelToolCall(call_id=f"c{i}", name="slow_p", arguments={}, raw_arguments="{}") for i in range(3)],
            )

        monkeypatch.setattr(
            parent.executor.llm_caller,
            "call_llm_async",
            AsyncMock(side_effect=make_parallel_response),
        )

        t0 = time.time()
        run_task = asyncio.create_task(parent.run_async(message="parallel"))
        await asyncio.sleep(0.5)
        assert len(tool_starts) >= 1, "至少一个 parallel tool worker 应该启动"

        parent.executor.force_stop()
        await asyncio.wait_for(run_task, timeout=6.0)
        elapsed = time.time() - t0

        assert elapsed < 2.0, f"主 agent 卡了 {elapsed:.2f}s, 期望 <2s (远小于 3s tool)"
