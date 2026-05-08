# Tool permission management primitives.
#
# RFC-0019: 工具权限管理
#
# 提供权限异常类型、ToolOutcome 数据类、匹配 helper 函数。

from .helpers import check_path_permission, check_permission, check_shell_permission, check_url_permission
from .types import (
    AllowOutcome,
    AskOutcome,
    AskPermission,
    DenyOutcome,
    PendingPermissionsError,
    PermissionDenied,
)

__all__ = [
    "AskPermission",
    "PermissionDenied",
    "PendingPermissionsError",
    "AllowOutcome",
    "DenyOutcome",
    "AskOutcome",
    "check_permission",
    "check_path_permission",
    "check_shell_permission",
    "check_url_permission",
]
