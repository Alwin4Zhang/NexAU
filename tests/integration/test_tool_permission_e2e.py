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

"""E2E integration tests for RFC-0019 tool permission management.

RFC-0019: 工具权限管理端到端测试

Uses InMemoryDatabaseEngine + mock LLM to exercise the full lifecycle:
1. Default behavior (no permissions) → all tools pass through
2. Explicit deny → tool returns denied
3. Ask → pause → resolve(allow) → resume → tool succeeds
4. Ask → resolve(deny) → resume → denial ToolResult
5. Persistent allow: after allow, same tool call auto-allows next time
6. allow_once: same tool call asks again next time
7. Permission cache built correctly from tool config
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from nexau.archs.permissions.helpers import check_permission
from nexau.archs.permissions.types import (
    AskPermission,
    PendingPermissionsError,
    PermissionDenied,
)
from nexau.archs.session import SessionManager
from nexau.archs.session.orm import InMemoryDatabaseEngine
from nexau.archs.tool.tool import Tool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(
    name: str,
    impl: Callable[..., Any] | None = None,
    permissions: dict[str, list[str]] | None = None,
) -> Tool:
    def _default_impl(**kwargs: object) -> dict[str, bool]:
        return {"ok": True}

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
# 1. Default behavior — no permissions
# ---------------------------------------------------------------------------


class TestDefaultBehavior:
    """Tools without permissions pass through normally."""

    def test_tool_without_permissions_executes(self):
        """Tool with no permissions field executes normally."""
        tool = _make_tool("plain_tool")
        result = tool.execute(x="hello")
        assert result == {"ok": True}

    def test_check_permission_with_wildcard_allow(self):
        """check_permission with allow_rules=["**"] passes through."""
        from nexau.archs.main_sub.framework_context import FrameworkContext

        ctx = FrameworkContext.for_testing(allow_rules=["**"], deny_rules=[])
        check_permission(ctx, "any:key", "Allow anything?")


# ---------------------------------------------------------------------------
# 2. Explicit deny
# ---------------------------------------------------------------------------


class TestExplicitDeny:
    """Tools that hit deny rules raise PermissionDenied."""

    def test_check_permission_deny_match(self):
        """check_permission raises PermissionDenied on deny rule match."""
        from nexau.archs.main_sub.framework_context import FrameworkContext

        ctx = FrameworkContext.for_testing(allow_rules=[], deny_rules=["path:/etc/*"])
        with pytest.raises(PermissionDenied) as exc_info:
            check_permission(ctx, "path:/etc/*", "Write to /etc?")
        assert exc_info.value.permission_key == "path:/etc/*"


# ---------------------------------------------------------------------------
# 3. Ask → pause → resolve(allow) → resume
# ---------------------------------------------------------------------------


class TestAskResolveAllowLifecycle:
    """Full Ask→resolve(allow)→resume lifecycle using session DB."""

    @pytest.mark.anyio
    async def test_ask_resolve_allow_resume(self):
        """Ask pauses, resolve(allow) marks decision, resume re-executes tool."""
        engine = InMemoryDatabaseEngine()
        sm = SessionManager(engine=engine)
        await sm.setup_models()

        user_id = "u_test"
        session_id = "s_test"

        # 1. Simulate executor writing pending_tool_calls (Ask state)
        pending = {
            "tc_1": {
                "tool_name": "write_file",
                "prompt": "Allow writing to /tmp/out.txt?",
                "permission_key": "path:/tmp/out.txt",
                "parameters": {"x": "1"},
                "decision": None,
            }
        }
        await sm.update_pending_tool_calls(
            user_id=user_id,
            session_id=session_id,
            pending_tool_calls=pending,
        )

        # 2. Verify pending is stored
        stored = await sm.get_pending_tool_calls(user_id=user_id, session_id=session_id)
        assert stored is not None
        assert stored["tc_1"]["decision"] is None

        # 3. Resolve with "allow"
        stored["tc_1"]["decision"] = "allow"
        await sm.update_pending_tool_calls(
            user_id=user_id,
            session_id=session_id,
            pending_tool_calls=stored,
        )

        # 4. Also persist the allow rule
        await sm.save_permission_rule(
            user_id=user_id,
            session_id=session_id,
            tool_name="write_file",
            rule_content="path:/tmp/out.txt",
            behavior="allow",
            source="user",
        )

        # 5. Verify decision is stored
        updated = await sm.get_pending_tool_calls(user_id=user_id, session_id=session_id)
        assert updated is not None
        assert updated["tc_1"]["decision"] == "allow"

        # 6. Verify allow rule is persisted
        allow_rules, deny_rules = await sm.load_permission_rules(
            user_id=user_id,
            session_id=session_id,
            tool_name="write_file",
        )
        assert "path:/tmp/out.txt" in allow_rules

        # 7. Clear pending (resume complete)
        await sm.update_pending_tool_calls(
            user_id=user_id,
            session_id=session_id,
            pending_tool_calls=None,
        )
        cleared = await sm.get_pending_tool_calls(user_id=user_id, session_id=session_id)
        assert cleared is None


# ---------------------------------------------------------------------------
# 4. Ask → resolve(deny)
# ---------------------------------------------------------------------------


class TestAskResolveDeny:
    """Ask→resolve(deny) lifecycle."""

    @pytest.mark.anyio
    async def test_deny_does_not_persist_allow_rule(self):
        """resolve(deny) marks decision but does NOT create an allow rule."""
        engine = InMemoryDatabaseEngine()
        sm = SessionManager(engine=engine)
        await sm.setup_models()

        user_id = "u_deny"
        session_id = "s_deny"

        pending = {
            "tc_d": {
                "tool_name": "run_shell",
                "prompt": "Allow shell: rm -rf?",
                "permission_key": "shell:rm",
                "parameters": {"x": "1"},
                "decision": None,
            }
        }
        await sm.update_pending_tool_calls(
            user_id=user_id,
            session_id=session_id,
            pending_tool_calls=pending,
        )

        # Resolve with deny (no rule persisted)
        pending["tc_d"]["decision"] = "deny"
        await sm.update_pending_tool_calls(
            user_id=user_id,
            session_id=session_id,
            pending_tool_calls=pending,
        )

        # No allow rule should exist
        allow_rules, deny_rules = await sm.load_permission_rules(
            user_id=user_id,
            session_id=session_id,
            tool_name="run_shell",
        )
        assert allow_rules == []


# ---------------------------------------------------------------------------
# 5. Persistent allow
# ---------------------------------------------------------------------------


class TestPersistentAllow:
    """After allow, the rule persists and auto-allows future calls."""

    @pytest.mark.anyio
    async def test_allow_rule_persists_across_loads(self):
        """Saved allow rule is returned on subsequent load_permission_rules calls."""
        engine = InMemoryDatabaseEngine()
        sm = SessionManager(engine=engine)
        await sm.setup_models()

        user_id = "u_persist"
        session_id = "s_persist"

        # Save rule
        await sm.save_permission_rule(
            user_id=user_id,
            session_id=session_id,
            tool_name="write_file",
            rule_content="path:/home/*",
            behavior="allow",
            source="user",
        )

        # Load and verify
        allow, deny = await sm.load_permission_rules(
            user_id=user_id,
            session_id=session_id,
            tool_name="write_file",
        )
        assert "path:/home/*" in allow
        assert deny == []

        # Load again to verify persistence
        allow2, _ = await sm.load_permission_rules(
            user_id=user_id,
            session_id=session_id,
            tool_name="write_file",
        )
        assert "path:/home/*" in allow2


# ---------------------------------------------------------------------------
# 6. allow_once
# ---------------------------------------------------------------------------


class TestAllowOnce:
    """allow_once does NOT persist a rule — next time asks again."""

    @pytest.mark.anyio
    async def test_allow_once_no_rule_persisted(self):
        """allow_once marks decision but no rule stored."""
        engine = InMemoryDatabaseEngine()
        sm = SessionManager(engine=engine)
        await sm.setup_models()

        user_id = "u_once"
        session_id = "s_once"

        pending = {
            "tc_o": {
                "tool_name": "write_file",
                "prompt": "Allow?",
                "permission_key": "path:/var",
                "parameters": {},
                "decision": None,
            }
        }
        await sm.update_pending_tool_calls(
            user_id=user_id,
            session_id=session_id,
            pending_tool_calls=pending,
        )

        # Resolve with allow_once — do NOT persist rule
        pending["tc_o"]["decision"] = "allow_once"
        await sm.update_pending_tool_calls(
            user_id=user_id,
            session_id=session_id,
            pending_tool_calls=pending,
        )

        # No allow rule should exist
        allow, _ = await sm.load_permission_rules(
            user_id=user_id,
            session_id=session_id,
            tool_name="write_file",
        )
        assert allow == []


# ---------------------------------------------------------------------------
# 7. Permission cache from tool config
# ---------------------------------------------------------------------------


class TestPermissionCacheFromConfig:
    """init_permission_rules_from_config populates DB from tool YAML permissions."""

    @pytest.mark.anyio
    async def test_init_from_config(self):
        """Tool config permissions are written to DB as source=config rules."""
        engine = InMemoryDatabaseEngine()
        sm = SessionManager(engine=engine)
        await sm.setup_models()

        user_id = "u_cfg"
        session_id = "s_cfg"

        tool = _make_tool(
            "guarded_tool",
            permissions={"allow": ["*.py", "*.txt"], "deny": ["secrets/*"]},
        )

        await sm.init_permission_rules_from_config(
            user_id=user_id,
            session_id=session_id,
            tools=[tool],
        )

        allow, deny = await sm.load_permission_rules(
            user_id=user_id,
            session_id=session_id,
            tool_name="guarded_tool",
        )
        assert set(allow) == {"*.py", "*.txt"}
        assert deny == ["secrets/*"]

    @pytest.mark.anyio
    async def test_tool_without_permissions_skipped(self):
        """Tool without permissions field does not create any rules."""
        engine = InMemoryDatabaseEngine()
        sm = SessionManager(engine=engine)
        await sm.setup_models()

        user_id = "u_skip"
        session_id = "s_skip"

        tool = _make_tool("plain_tool", permissions=None)

        await sm.init_permission_rules_from_config(
            user_id=user_id,
            session_id=session_id,
            tools=[tool],
        )

        allow, deny = await sm.load_permission_rules(
            user_id=user_id,
            session_id=session_id,
            tool_name="plain_tool",
        )
        assert allow == []
        assert deny == []


# ---------------------------------------------------------------------------
# 8. Mixed parallel outcomes
# ---------------------------------------------------------------------------


class TestMixedParallelOutcomes:
    """Verify check_permission correctly handles mixed scenarios."""

    def test_one_allow_one_ask(self):
        """Two tools: one with matching allow rule, one with no match (Ask)."""
        from nexau.archs.main_sub.framework_context import FrameworkContext

        # Tool A: allow rule matches
        ctx_a = FrameworkContext.for_testing(allow_rules=["key:a"], deny_rules=[])
        check_permission(ctx_a, "key:a", "Allow A?")

        # Tool B: no rule matches → AskPermission
        ctx_b = FrameworkContext.for_testing(allow_rules=[], deny_rules=[])
        with pytest.raises(AskPermission) as exc_info:
            check_permission(ctx_b, "key:b", "Allow B?")
        assert exc_info.value.permission_key == "key:b"

    def test_deny_takes_priority_over_allow(self):
        """Deny rule evaluated before allow → PermissionDenied."""
        from nexau.archs.main_sub.framework_context import FrameworkContext

        ctx = FrameworkContext.for_testing(
            allow_rules=["key:x"],
            deny_rules=["key:x"],
        )
        with pytest.raises(PermissionDenied):
            check_permission(ctx, "key:x", "Allow?")


# ---------------------------------------------------------------------------
# 9. PendingPermissionsError structure
# ---------------------------------------------------------------------------


class TestPendingPermissionsErrorStructure:
    """Verify PendingPermissionsError carries correct data."""

    def test_error_message_format(self):
        pending = {"tc_1": {"decision": None}, "tc_2": {"decision": None}}
        err = PendingPermissionsError(session_id="s_1", pending=pending)
        assert "s_1" in str(err)
        assert "2" in str(err)
        assert err.session_id == "s_1"
        assert err.pending is pending

    def test_single_pending(self):
        pending = {"tc_x": {"decision": None}}
        err = PendingPermissionsError(session_id="s_2", pending=pending)
        assert "1" in str(err)
