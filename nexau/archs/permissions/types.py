# Permission types for tool permission management.
#
# RFC-0019: 工具权限管理
#
# 定义权限系统的异常类型和 ToolOutcome 数据类。
# Tool 函数通过 raise AskPermission / PermissionDenied 与框架通信，
# Executor 将异常收集为 ToolOutcome 后统一处理。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexau.archs.main_sub.execution.tool_executor import ToolExecutionResult


class AskPermission(Exception):  # noqa: N818 — signal, not error
    """Tool 函数在匹配不到 allow/deny 规则时 raise。

    RFC-0019: Ask 触发异常

    携带 prompt（展示给用户的描述）和 permission_key（用于写 allow 规则）。
    tool_call_id / tool_name 由 executor 从调用上下文补充。
    """

    def __init__(self, *, prompt: str, permission_key: str) -> None:
        self.prompt = prompt
        self.permission_key = permission_key
        super().__init__(prompt)


class PermissionDenied(Exception):  # noqa: N818 — signal, not error
    """Tool 函数在命中 deny 规则时 raise。

    RFC-0019: Deny 触发异常
    """

    def __init__(self, *, reason: str, permission_key: str) -> None:
        self.reason = reason
        self.permission_key = permission_key
        super().__init__(reason)


class PendingPermissionsError(Exception):
    """agent.run() 检测到未决 pending_tool_calls 时 raise。

    RFC-0019: 硬拦规则

    防止在有未决权限请求时启动新 run。
    """

    def __init__(self, *, session_id: str, pending: dict[str, Any]) -> None:
        self.session_id = session_id
        self.pending = pending
        super().__init__(
            f"Session {session_id} has {len(pending)} pending permission request(s). Resolve all decisions before starting a new run."
        )


# ---------------------------------------------------------------------------
# ToolOutcome: Executor 将 tool 执行结果统一收集为 ToolOutcome
# ---------------------------------------------------------------------------


@dataclass
class AllowOutcome:
    """Tool 正常执行完毕。

    RFC-0019: 三态之一 — Allow
    """

    tool_call_id: str
    result: ToolExecutionResult


@dataclass
class DenyOutcome:
    """Tool 命中 deny 规则被拒绝。

    RFC-0019: 三态之一 — Deny
    """

    tool_call_id: str
    reason: str
    permission_key: str


@dataclass
class AskOutcome:
    """Tool 需要用户确认权限。

    RFC-0019: 三态之一 — Ask

    携带原始调用参数，以便 resume 时重新调用 tool。
    """

    tool_call_id: str
    tool_name: str
    prompt: str
    permission_key: str
    parameters: dict[str, Any] = field(default_factory=lambda: {})
