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

"""finish_team tool — end the team collaboration session.

RFC-0002: 结束团队协作会话
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nexau.archs.main_sub.team.types import (
    FinishTeamResult,
    ToolError,
    require_team_state,
)

if TYPE_CHECKING:
    from nexau.archs.main_sub.agent_state import AgentState


async def finish_team(
    summary: str,
    agent_state: AgentState,
) -> FinishTeamResult | ToolError:
    """Finish the team collaboration session.

    RFC-0002: 结束团队协作会话

    仅 leader 可调用。统计任务完成情况并返回摘要。
    作为 stop tool 注册，调用后 leader 的 executor 循环退出。
    """
    ts = require_team_state(agent_state)

    if not ts.is_leader:
        return ToolError(
            error="Only the team leader can finish the team",
            code="permission_denied",
        )

    # 1. 统计任务完成情况
    all_tasks = await ts.task_board.list_tasks()
    completed = [t for t in all_tasks if t.status == "completed"]
    incomplete = [t for t in all_tasks if t.status != "completed"]

    # 2. 强制停止仍在运行的 teammate（leader 决定收工即可终止 worker）
    teammates = ts.team.get_teammate_info()
    running_teammates = [t for t in teammates if t.status == "running"]
    if running_teammates:
        await ts.team.stop_all_teammates()

    # 3. 阻止结束：存在未完成任务
    if incomplete:
        task_details = ", ".join(f"{t.title}({t.status})" for t in incomplete)
        return ToolError(
            error=f"Cannot finish team: {len(incomplete)} incomplete task(s): {task_details}",
            code="invalid_state",
        )

    return FinishTeamResult(
        summary=summary,
        completed_tasks=len(completed),
        total_tasks=len(all_tasks),
    )
