# RFC-0019: Permission rules model and session manager tests

import asyncio
from types import SimpleNamespace

import pytest

from nexau.archs.session.models import PermissionRuleModel, SessionModel
from nexau.archs.session.orm import InMemoryDatabaseEngine
from nexau.archs.session.session_manager import SessionManager


@pytest.fixture
def engine() -> InMemoryDatabaseEngine:
    return InMemoryDatabaseEngine()


@pytest.fixture
def manager(engine: InMemoryDatabaseEngine) -> SessionManager:
    return SessionManager(engine=engine)


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class TestPermissionRuleModel:
    def test_create_and_query(self, engine: InMemoryDatabaseEngine) -> None:
        async def run() -> None:
            await engine.setup_models([PermissionRuleModel])
            rule = PermissionRuleModel(
                user_id="u1",
                session_id="s1",
                tool_name="run_shell_command",
                rule_content="ls",
                behavior="allow",
                source="config",
            )
            await engine.create(rule)
            loaded = await engine.find_many(PermissionRuleModel)
            assert len(loaded) == 1
            assert loaded[0].tool_name == "run_shell_command"
            assert loaded[0].behavior == "allow"

        _run(run())

    def test_multiple_rules_per_tool(self, engine: InMemoryDatabaseEngine) -> None:
        async def run() -> None:
            await engine.setup_models([PermissionRuleModel])
            for content in ["ls", "cat", "grep"]:
                await engine.create(
                    PermissionRuleModel(
                        user_id="u1",
                        session_id="s1",
                        tool_name="shell",
                        rule_content=content,
                        behavior="allow",
                        source="config",
                    )
                )
            await engine.create(
                PermissionRuleModel(
                    user_id="u1",
                    session_id="s1",
                    tool_name="shell",
                    rule_content="rm",
                    behavior="deny",
                    source="config",
                )
            )
            loaded = await engine.find_many(PermissionRuleModel)
            assert len(loaded) == 4
            allow_rules = [r.rule_content for r in loaded if r.behavior == "allow"]
            deny_rules = [r.rule_content for r in loaded if r.behavior == "deny"]
            assert set(allow_rules) == {"ls", "cat", "grep"}
            assert deny_rules == ["rm"]

        _run(run())


class TestSessionPendingToolCalls:
    def test_pending_tool_calls_default_none(self, engine: InMemoryDatabaseEngine) -> None:
        async def run() -> None:
            await engine.setup_models([SessionModel])
            session = SessionModel(user_id="u1", session_id="s1")
            await engine.create(session)
            loaded = await engine.find_many(SessionModel)
            assert len(loaded) == 1
            assert loaded[0].pending_tool_calls is None

        _run(run())

    def test_pending_tool_calls_round_trip(self, engine: InMemoryDatabaseEngine) -> None:
        async def run() -> None:
            await engine.setup_models([SessionModel])
            pending = {
                "tc_abc": {
                    "tool_name": "run_shell_command",
                    "prompt": "允许执行 npm install 吗?",
                    "permission_key": "npm",
                    "decision": None,
                }
            }
            session = SessionModel(user_id="u1", session_id="s1", pending_tool_calls=pending)
            await engine.create(session)
            loaded = await engine.find_many(SessionModel)
            assert loaded[0].pending_tool_calls is not None
            assert loaded[0].pending_tool_calls["tc_abc"]["tool_name"] == "run_shell_command"
            assert loaded[0].pending_tool_calls["tc_abc"]["decision"] is None

        _run(run())


class TestSessionManagerPermissions:
    def test_load_empty_rules(self, manager: SessionManager) -> None:
        async def run() -> None:
            await manager.setup_models()
            allow, deny = await manager.load_permission_rules(user_id="u1", session_id="s1", tool_name="shell")
            assert allow == []
            assert deny == []

        _run(run())

    def test_save_and_load_rules(self, manager: SessionManager) -> None:
        async def run() -> None:
            await manager.setup_models()
            await manager.save_permission_rule(
                user_id="u1",
                session_id="s1",
                tool_name="shell",
                rule_content="ls",
                behavior="allow",
            )
            await manager.save_permission_rule(
                user_id="u1",
                session_id="s1",
                tool_name="shell",
                rule_content="rm",
                behavior="deny",
            )
            allow, deny = await manager.load_permission_rules(user_id="u1", session_id="s1", tool_name="shell")
            assert allow == ["ls"]
            assert deny == ["rm"]

        _run(run())

    def test_init_from_config(self, manager: SessionManager) -> None:
        async def run() -> None:
            await manager.setup_models()
            # 模拟 Tool 对象
            tool = SimpleNamespace(
                name="run_shell_command",
                permissions={"allow": ["ls", "cat"], "deny": ["rm"]},
            )
            tool_no_perm = SimpleNamespace(name="read_file", permissions=None)

            await manager.init_permission_rules_from_config(
                user_id="u1",
                session_id="s1",
                tools=[tool, tool_no_perm],
            )
            allow, deny = await manager.load_permission_rules(user_id="u1", session_id="s1", tool_name="run_shell_command")
            assert set(allow) == {"ls", "cat"}
            assert deny == ["rm"]

            # tool without permissions should have no rules
            allow2, deny2 = await manager.load_permission_rules(user_id="u1", session_id="s1", tool_name="read_file")
            assert allow2 == []
            assert deny2 == []

        _run(run())

    def test_pending_tool_calls_crud(self, manager: SessionManager) -> None:
        async def run() -> None:
            await manager.setup_models()
            # 先创建 session
            await manager.register_agent(user_id="u1", session_id="s1", agent_name="test")

            # 初始应该是 None
            pending = await manager.get_pending_tool_calls(user_id="u1", session_id="s1")
            assert pending is None

            # 写入 pending
            new_pending = {
                "tc_1": {
                    "tool_name": "shell",
                    "prompt": "allow npm?",
                    "permission_key": "npm",
                    "decision": None,
                }
            }
            await manager.update_pending_tool_calls(user_id="u1", session_id="s1", pending_tool_calls=new_pending)

            # 读取
            loaded = await manager.get_pending_tool_calls(user_id="u1", session_id="s1")
            assert loaded is not None
            assert "tc_1" in loaded
            assert loaded["tc_1"]["decision"] is None

            # 清除
            await manager.update_pending_tool_calls(user_id="u1", session_id="s1", pending_tool_calls=None)
            cleared = await manager.get_pending_tool_calls(user_id="u1", session_id="s1")
            assert cleared is None

        _run(run())

    def test_upsert_prevents_duplicate(self, manager: SessionManager) -> None:
        async def run() -> None:
            await manager.setup_models()
            for _ in range(3):
                await manager.save_permission_rule(
                    user_id="u1",
                    session_id="s1",
                    tool_name="shell",
                    rule_content="ls",
                    behavior="allow",
                )
            allow, _ = await manager.load_permission_rules(user_id="u1", session_id="s1", tool_name="shell")
            assert allow == ["ls"]

        _run(run())
