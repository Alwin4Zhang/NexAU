# Permission rules model for tool permission management.
#
# RFC-0019: 工具权限管理
#
# permission_rules 表持久化 allow/deny 规则。
# Session 创建时从 tool YAML 配置写入 source=config 的初始规则，
# 用户 allow 决策追加 source=user 的规则。

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel


class PermissionRuleModel(SQLModel, table=True):
    """Permission rule for a tool in a session.

    RFC-0019: permission_rules 表

    复合主键 (user_id, session_id, tool_name, rule_content, behavior)
    确保同一 session 内同一 tool 的同一规则不会重复。
    """

    __tablename__ = "permission_rules"  # type: ignore[assignment]

    user_id: str = Field(primary_key=True)
    session_id: str = Field(primary_key=True)
    tool_name: str = Field(primary_key=True)
    rule_content: str = Field(primary_key=True)
    behavior: str = Field(primary_key=True)
    source: str = Field(default="config")
    created_at: datetime = Field(default_factory=datetime.now)
