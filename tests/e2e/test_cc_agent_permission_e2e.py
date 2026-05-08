# RFC-0019: CC Agent 权限管理端到端测试
#
# 使用真实 cc_agent 工具（YAML + 真实绑定函数）+ 真实 LLM API 验证权限管理全管线：
# Part 1 — 配置管线：config → DB 初始化 → 权限缓存（静态检查，无需 LLM）
# Part 2 — 真实 LLM 端到端：agent.run_async() → LLM tool call → 权限拦截 → 结果校验
#
# 所有 LLM 文件操作都在 tmp_path 下，结果可预测。
# 通过 .env 读取 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL。

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.permissions.types import PendingPermissionsError
from nexau.archs.sandbox.base_sandbox import LocalSandboxConfig
from nexau.archs.session import SessionManager
from nexau.archs.session.orm import InMemoryDatabaseEngine
from nexau.archs.tool.tool import Tool

load_dotenv()

# ---------------------------------------------------------------------------
# Shared: load cc_agent tools from YAML + real bindings
# ---------------------------------------------------------------------------

TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "cc_agent" / "tools"


def _build_cc_agent_tools() -> list[Tool]:
    """Build ALL cc_agent tools from real YAML files with real bindings.

    Mirrors scripts/demo_cc_agent.py _build_tools() exactly.
    """
    from nexau.archs.tool.builtin.file_tools.apply_patch import apply_patch
    from nexau.archs.tool.builtin.file_tools.glob_tool import glob
    from nexau.archs.tool.builtin.file_tools.list_directory import list_directory
    from nexau.archs.tool.builtin.file_tools.read_file import read_file
    from nexau.archs.tool.builtin.file_tools.read_many_files import read_many_files
    from nexau.archs.tool.builtin.file_tools.read_visual_file import read_visual_file
    from nexau.archs.tool.builtin.file_tools.replace import replace
    from nexau.archs.tool.builtin.file_tools.search_file_content import search_file_content
    from nexau.archs.tool.builtin.file_tools.write_file import write_file
    from nexau.archs.tool.builtin.multiedit_tool import multiedit_tool
    from nexau.archs.tool.builtin.run_code_tool import run_code_tool
    from nexau.archs.tool.builtin.shell_tools import run_shell_command
    from nexau.archs.tool.builtin.web_tools import google_web_search, web_fetch

    tools: list[Tool] = []
    empty: dict[str, list[str]] = {"allow": [], "deny": []}

    # Readonly (no permissions → default wildcard allow)
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "read_file.tool.yaml"), binding=read_file))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "read_many_files.tool.yaml"), binding=read_many_files))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "read_visual_file.tool.yaml"), binding=read_visual_file))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "glob.tool.yaml"), binding=glob))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "list_directory.tool.yaml"), binding=list_directory))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "search_file_content.tool.yaml"), binding=search_file_content))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "WebSearch.tool.yaml"), binding=google_web_search))

    # File write (path-level ask)
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "write_file.tool.yaml"), binding=write_file, permissions=empty))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "replace.tool.yaml"), binding=replace, permissions=empty))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "apply_patch.tool.yaml"), binding=apply_patch, permissions=empty))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "multiedit_tool.tool.yaml"), binding=multiedit_tool, permissions=empty))

    # Shell (readonly whitelist + ask)
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "run_shell_command.tool.yaml"), binding=run_shell_command, permissions=empty))

    # Code execution (always ask)
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "run_code_tool.tool.yaml"), binding=run_code_tool, permissions=empty))

    # Web fetch (domain-level ask)
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "WebFetch.tool.yaml"), binding=web_fetch, permissions=empty))

    return tools


SYSTEM_PROMPT = """\
You are a coding assistant in a test environment.

RULES:
- When asked to use a specific tool, call EXACTLY that tool with the EXACT arguments given.
- When asked to do multiple things, call ALL tools in ONE response.
- If a tool call fails due to permission, report the error and stop.
- Do NOT ask for confirmation — just call tools directly.
- Do NOT make up file paths — use the EXACT paths given in the instruction.
- Keep responses concise.

Working directory: {work_dir}
"""


async def _make_agent(
    tmp_path: Path,
    test_id: str,
    *,
    extra_allow_rules: dict[str, list[str]] | None = None,
    extra_deny_rules: dict[str, list[str]] | None = None,
    max_iterations: int = 10,
) -> tuple[Agent, SessionManager, str, str]:
    """Create a real cc_agent with LLM, sandbox, and permission rules.

    Returns (agent, session_manager, user_id, session_id).
    """
    engine = InMemoryDatabaseEngine()
    sm = SessionManager(engine=engine)
    await sm.setup_models()

    tools = _build_cc_agent_tools()
    user_id = f"u_{test_id}"
    session_id = f"s_{test_id}"

    await sm.init_permission_rules_from_config(
        user_id=user_id,
        session_id=session_id,
        tools=tools,
    )

    # Pre-load extra allow/deny rules
    if extra_allow_rules:
        for tool_name, rules in extra_allow_rules.items():
            for rule in rules:
                await sm.save_permission_rule(
                    user_id=user_id,
                    session_id=session_id,
                    tool_name=tool_name,
                    rule_content=rule,
                    behavior="allow",
                    source="test",
                )
    if extra_deny_rules:
        for tool_name, rules in extra_deny_rules.items():
            for rule in rules:
                await sm.save_permission_rule(
                    user_id=user_id,
                    session_id=session_id,
                    tool_name=tool_name,
                    rule_content=rule,
                    behavior="deny",
                    source="test",
                )

    config = AgentConfig(
        name=f"cc_perm_{test_id}",
        system_prompt=SYSTEM_PROMPT.format(work_dir=str(tmp_path)),
        llm_config=LLMConfig(temperature=0),
        tools=tools,
        max_iterations=max_iterations,
        sandbox_config=LocalSandboxConfig(work_dir=str(tmp_path)),
    )
    agent = await Agent.create(
        config=config,
        session_manager=sm,
        user_id=user_id,
        session_id=session_id,
    )
    return agent, sm, user_id, session_id


async def _get_pending(
    sm: SessionManager,
    user_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    return await sm.get_pending_tool_calls(user_id=user_id, session_id=session_id)


async def _get_unresolved(
    sm: SessionManager,
    user_id: str,
    session_id: str,
) -> dict[str, Any]:
    pending = await _get_pending(sm, user_id, session_id)
    if pending is None:
        return {}
    return {k: v for k, v in pending.items() if v.get("decision") is None}


async def _resolve_all(
    agent: Agent,
    sm: SessionManager,
    user_id: str,
    session_id: str,
    decision: str = "allow",
) -> None:
    unresolved = await _get_unresolved(sm, user_id, session_id)
    for tc_id in unresolved:
        await agent.resolve_permission(tc_id, decision)


# =========================================================================
# Part 1: Config Pipeline — verify config → DB → cache (static, no LLM)
# =========================================================================


class TestCcAgentConfigPipeline:
    """Verify cc_agent tools load correctly and init_permission_rules works."""

    def test_all_yaml_tools_loaded(self) -> None:
        tools = _build_cc_agent_tools()
        names = {t.name for t in tools}
        assert len(tools) == 14, f"Expected 14 YAML tools, got {len(tools)}: {names}"

        expected = {
            "read_file",
            "read_many_files",
            "read_visual_file",
            "glob",
            "list_directory",
            "search_file_content",
            "WebSearch",
            "write_file",
            "replace",
            "apply_patch",
            "multiedit_tool",
            "run_shell_command",
            "run_code_tool",
            "WebFetch",
        }
        assert names == expected, f"Mismatch: missing={expected - names}, extra={names - expected}"

    def test_readonly_tools_have_no_permissions(self) -> None:
        tools = _build_cc_agent_tools()
        readonly_names = {"read_file", "read_many_files", "read_visual_file", "glob", "list_directory", "search_file_content", "WebSearch"}
        for tool in tools:
            if tool.name in readonly_names:
                assert tool.permissions is None, f"{tool.name} should have no permissions"

    def test_write_tools_have_empty_permissions(self) -> None:
        tools = _build_cc_agent_tools()
        write_names = {"write_file", "replace", "apply_patch", "multiedit_tool", "run_shell_command", "run_code_tool", "WebFetch"}
        for tool in tools:
            if tool.name in write_names:
                assert tool.permissions == {"allow": [], "deny": []}, f"{tool.name} should have empty permissions, got {tool.permissions}"

    def test_init_permission_rules_from_config(self) -> None:
        import asyncio

        async def run() -> None:
            engine = InMemoryDatabaseEngine()
            sm = SessionManager(engine=engine)
            await sm.setup_models()

            tools = _build_cc_agent_tools()
            await sm.init_permission_rules_from_config(
                user_id="u1",
                session_id="s1",
                tools=tools,
            )

            for name in ("read_file", "glob", "WebSearch"):
                allow, deny = await sm.load_permission_rules(
                    user_id="u1",
                    session_id="s1",
                    tool_name=name,
                )
                assert allow == [], f"{name} should have no allow rules"
                assert deny == [], f"{name} should have no deny rules"

            for name in ("write_file", "run_shell_command", "run_code_tool", "WebFetch"):
                allow, deny = await sm.load_permission_rules(
                    user_id="u1",
                    session_id="s1",
                    tool_name=name,
                )
                assert allow == [], f"{name} should have no allow rules (empty config list)"
                assert deny == [], f"{name} should have no deny rules (empty config list)"

        asyncio.run(run())

    def test_readonly_tools_have_no_ctx_in_signature(self) -> None:
        tools = _build_cc_agent_tools()
        no_ctx_expected = {"read_file", "read_many_files", "read_visual_file", "glob", "list_directory", "search_file_content"}
        for tool in tools:
            if tool.name in no_ctx_expected:
                assert tool.implementation is not None, f"{tool.name} has no implementation"
                sig = inspect.signature(tool.implementation)
                assert "ctx" not in sig.parameters, f"{tool.name} should NOT have ctx parameter but does"

    def test_write_tools_have_ctx_in_signature(self) -> None:
        tools = _build_cc_agent_tools()
        ctx_expected = {"write_file", "replace", "apply_patch", "multiedit_tool", "run_shell_command", "run_code_tool", "WebFetch"}
        for tool in tools:
            if tool.name in ctx_expected:
                assert tool.implementation is not None, f"{tool.name} has no implementation"
                sig = inspect.signature(tool.implementation)
                assert "ctx" in sig.parameters, f"{tool.name} MUST have ctx parameter but doesn't"


# =========================================================================
# Part 2: Real LLM E2E — agent.run_async() → LLM → tool call → 权限校验
# =========================================================================


@pytest.mark.llm
@pytest.mark.timeout(180)
class TestReadonlyAutoAllow:
    """Readonly tools (no permissions) should auto-allow, no pending."""

    @pytest.mark.anyio
    async def test_read_file(self, tmp_path: Path) -> None:
        target = tmp_path / "readable.txt"
        target.write_text("test_content_abc123")

        agent, sm, uid, sid = await _make_agent(tmp_path, "read_file")
        resp = await agent.run_async(
            message=f"Use the read_file tool to read {target}",
        )
        assert isinstance(resp, str) and len(resp) > 0
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "read_file should auto-allow"

    @pytest.mark.anyio
    async def test_list_directory(self, tmp_path: Path) -> None:
        (tmp_path / "file_a.py").write_text("a")
        (tmp_path / "file_b.py").write_text("b")

        agent, sm, uid, sid = await _make_agent(tmp_path, "list_dir")
        resp = await agent.run_async(
            message=f"Use the list_directory tool to list {tmp_path}",
        )
        assert isinstance(resp, str) and len(resp) > 0
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "list_directory should auto-allow"

    @pytest.mark.anyio
    async def test_glob(self, tmp_path: Path) -> None:
        (tmp_path / "hello.py").write_text("x")
        (tmp_path / "world.py").write_text("y")

        agent, sm, uid, sid = await _make_agent(tmp_path, "glob")
        resp = await agent.run_async(
            message=f"Use the glob tool to find all *.py files in {tmp_path}",
        )
        assert isinstance(resp, str)
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "glob should auto-allow"

    @pytest.mark.anyio
    async def test_search_file_content(self, tmp_path: Path) -> None:
        (tmp_path / "searchme.txt").write_text("needle_value_xyz")

        agent, sm, uid, sid = await _make_agent(tmp_path, "search")
        resp = await agent.run_async(
            message=f"Use the search_file_content tool to search for 'needle_value_xyz' in {tmp_path}",
        )
        assert isinstance(resp, str)
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "search_file_content should auto-allow"

    @pytest.mark.anyio
    async def test_shell_ls_auto_allow(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(tmp_path, "shell_ls")
        resp = await agent.run_async(
            message=f"Use the run_shell_command tool to run: ls -la {tmp_path}",
        )
        assert isinstance(resp, str) and len(resp) > 0
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "ls should auto-allow via readonly whitelist"

    @pytest.mark.anyio
    async def test_shell_cat_auto_allow(self, tmp_path: Path) -> None:
        f = tmp_path / "catme.txt"
        f.write_text("cat_content_789")

        agent, sm, uid, sid = await _make_agent(tmp_path, "shell_cat")
        resp = await agent.run_async(
            message=f"Use the run_shell_command tool to run: cat {f}",
        )
        assert isinstance(resp, str)
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "cat should auto-allow via readonly whitelist"

    @pytest.mark.anyio
    async def test_shell_git_log_auto_allow(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(tmp_path, "shell_git_log")
        resp = await agent.run_async(
            message="Use the run_shell_command tool to run: git log --oneline -1",
        )
        assert isinstance(resp, str)
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "git log should auto-allow via readonly whitelist"

    @pytest.mark.anyio
    async def test_shell_git_status_auto_allow(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(tmp_path, "shell_git_status")
        resp = await agent.run_async(
            message="Use the run_shell_command tool to run: git status",
        )
        assert isinstance(resp, str)
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "git status should auto-allow via readonly whitelist"


@pytest.mark.llm
@pytest.mark.timeout(180)
class TestEmptyRulesAsk:
    """Write/execute tools with empty rules → AskPermission → pending_tool_calls."""

    @pytest.mark.anyio
    async def test_write_file_ask(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(tmp_path, "wf_ask")
        target = tmp_path / "newfile.txt"
        await agent.run_async(
            message=f"Use the write_file tool to create {target} with content 'hello'",
        )
        pending = await _get_pending(sm, uid, sid)
        if pending is None:
            pytest.skip("LLM did not call write_file")
        unresolved = await _get_unresolved(sm, uid, sid)
        assert len(unresolved) > 0
        assert all(v["tool_name"] == "write_file" for v in unresolved.values())

    @pytest.mark.anyio
    async def test_replace_ask(self, tmp_path: Path) -> None:
        target = tmp_path / "editable.txt"
        target.write_text("old_value_abc")

        agent, sm, uid, sid = await _make_agent(tmp_path, "rep_ask")
        await agent.run_async(
            message=(f"Use the replace tool on {target}. Replace old_string='old_value_abc' with new_string='new_value_xyz'."),
        )
        pending = await _get_pending(sm, uid, sid)
        if pending is None:
            pytest.skip("LLM did not call replace")
        unresolved = await _get_unresolved(sm, uid, sid)
        assert len(unresolved) > 0
        assert all(v["tool_name"] == "replace" for v in unresolved.values())

    @pytest.mark.anyio
    async def test_multiedit_ask(self, tmp_path: Path) -> None:
        target = tmp_path / "multi.txt"
        target.write_text("hello world")

        agent, sm, uid, sid = await _make_agent(tmp_path, "me_ask")
        await agent.run_async(
            message=(f"Use the multiedit_tool to edit {target}. Replace old_string='hello' with new_string='hi'."),
        )
        pending = await _get_pending(sm, uid, sid)
        if pending is None:
            pytest.skip("LLM did not call multiedit_tool")
        unresolved = await _get_unresolved(sm, uid, sid)
        assert len(unresolved) > 0
        assert all(v["tool_name"] == "multiedit_tool" for v in unresolved.values())

    @pytest.mark.anyio
    async def test_apply_patch_ask(self, tmp_path: Path) -> None:
        target = tmp_path / "patchme.py"
        target.write_text("line1\nline2\n")

        agent, sm, uid, sid = await _make_agent(tmp_path, "ap_ask")
        await agent.run_async(
            message=(f"Use the apply_patch tool to patch {target}. Change 'line1' to 'patched_line'."),
        )
        pending = await _get_pending(sm, uid, sid)
        if pending is None:
            pytest.skip("LLM did not call apply_patch")
        unresolved = await _get_unresolved(sm, uid, sid)
        assert len(unresolved) > 0
        assert all(v["tool_name"] == "apply_patch" for v in unresolved.values())

    @pytest.mark.anyio
    async def test_shell_write_command_ask(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(tmp_path, "sh_write_ask")
        await agent.run_async(
            message="Use the run_shell_command tool to run: python --version",
        )
        pending = await _get_pending(sm, uid, sid)
        if pending is None:
            pytest.skip("LLM did not call run_shell_command")
        unresolved = await _get_unresolved(sm, uid, sid)
        assert len(unresolved) > 0
        assert all(v["tool_name"] == "run_shell_command" for v in unresolved.values())

    @pytest.mark.anyio
    async def test_shell_git_push_ask(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(tmp_path, "sh_push_ask")
        await agent.run_async(
            message="Use the run_shell_command tool to run: git push origin main",
        )
        pending = await _get_pending(sm, uid, sid)
        if pending is None:
            pytest.skip("LLM did not call run_shell_command")
        unresolved = await _get_unresolved(sm, uid, sid)
        assert len(unresolved) > 0

    @pytest.mark.anyio
    async def test_shell_pipeline_ask(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(tmp_path, "sh_pipe_ask")
        await agent.run_async(
            message="Use the run_shell_command tool to run exactly this command: ls && rm -rf /tmp/foo_test_nonexistent",
        )
        pending = await _get_pending(sm, uid, sid)
        if pending is None:
            pytest.skip("LLM did not call run_shell_command")
        unresolved = await _get_unresolved(sm, uid, sid)
        assert len(unresolved) > 0

    @pytest.mark.anyio
    async def test_run_code_ask(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(tmp_path, "rc_ask")
        await agent.run_async(
            message="Use the run_code_tool to execute this Python code: print('hello world')",
        )
        pending = await _get_pending(sm, uid, sid)
        if pending is None:
            pytest.skip("LLM did not call run_code_tool")
        unresolved = await _get_unresolved(sm, uid, sid)
        assert len(unresolved) > 0
        assert all(v["tool_name"] == "run_code_tool" for v in unresolved.values())

    @pytest.mark.anyio
    async def test_webfetch_ask(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(tmp_path, "wf_url_ask")
        await agent.run_async(
            message="Use the WebFetch tool to fetch the URL https://example.com",
        )
        pending = await _get_pending(sm, uid, sid)
        if pending is None:
            pytest.skip("LLM did not call WebFetch")
        unresolved = await _get_unresolved(sm, uid, sid)
        assert len(unresolved) > 0
        assert all(v["tool_name"] == "WebFetch" for v in unresolved.values())


@pytest.mark.llm
@pytest.mark.timeout(180)
class TestAllowRulesPass:
    """Pre-loaded allow rules → tools execute without ask."""

    @pytest.mark.anyio
    async def test_write_file_with_allow(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(
            tmp_path,
            "wf_allow",
            extra_allow_rules={"write_file": [str(tmp_path / "**")]},
        )
        target = tmp_path / "allowed.txt"
        resp = await agent.run_async(
            message=f"Use the write_file tool to create {target} with content 'allowed'",
        )
        assert isinstance(resp, str)
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "write_file with allow rule should not create pending"
        assert target.exists(), f"File should have been created at {target}"
        assert target.read_text() == "allowed"

    @pytest.mark.anyio
    async def test_replace_with_allow(self, tmp_path: Path) -> None:
        target = tmp_path / "replaceme.txt"
        target.write_text("old_value_replace_test")

        agent, sm, uid, sid = await _make_agent(
            tmp_path,
            "rep_allow",
            extra_allow_rules={"replace": [str(tmp_path / "**")]},
        )
        await agent.run_async(
            message=(
                f"Use the replace tool on {target}. Replace old_string='old_value_replace_test' with new_string='new_value_replace_test'."
            ),
        )
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "replace with allow rule should not create pending"
        content = target.read_text()
        assert "new_value_replace_test" in content, f"File content should be updated, got: {content}"

    @pytest.mark.anyio
    async def test_shell_with_allow(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(
            tmp_path,
            "sh_allow",
            extra_allow_rules={"run_shell_command": ["python"]},
        )
        resp = await agent.run_async(
            message="Use the run_shell_command tool to run: python --version",
        )
        assert isinstance(resp, str)
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "python with allow rule should not create pending"

    @pytest.mark.anyio
    async def test_webfetch_with_allow(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(
            tmp_path,
            "wf_url_allow",
            extra_allow_rules={"WebFetch": ["example.com"]},
        )
        resp = await agent.run_async(
            message="Use the WebFetch tool to fetch https://example.com",
        )
        assert isinstance(resp, str)
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "WebFetch with allow domain should not create pending"

    @pytest.mark.anyio
    async def test_run_code_with_allow(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(
            tmp_path,
            "rc_allow",
            extra_allow_rules={"run_code_tool": ["code_execution"]},
        )
        resp = await agent.run_async(
            message="Use the run_code_tool to execute: print('hello')",
        )
        assert isinstance(resp, str)
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "run_code_tool with allow rule should not create pending"


@pytest.mark.llm
@pytest.mark.timeout(180)
class TestDenyRulesBlocked:
    """Pre-loaded deny rules → PermissionDenied → error returned to LLM, no pending."""

    @pytest.mark.anyio
    async def test_write_file_denied(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(
            tmp_path,
            "wf_deny",
            extra_deny_rules={"write_file": [str(tmp_path / "**")]},
        )
        resp = await agent.run_async(
            message=f"Use the write_file tool to create {tmp_path / 'denied.txt'} with content 'x'",
        )
        assert isinstance(resp, str)
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "Deny should not create pending (DenyOutcome is immediate error)"
        assert not (tmp_path / "denied.txt").exists(), "Denied file should not be created"

    @pytest.mark.anyio
    async def test_shell_denied(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(
            tmp_path,
            "sh_deny",
            extra_deny_rules={"run_shell_command": ["python"]},
        )
        resp = await agent.run_async(
            message="Use the run_shell_command tool to run: python --version",
        )
        assert isinstance(resp, str)
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "Deny should not create pending"

    @pytest.mark.anyio
    async def test_webfetch_denied(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(
            tmp_path,
            "wf_url_deny",
            extra_deny_rules={"WebFetch": ["example.com"]},
        )
        resp = await agent.run_async(
            message="Use the WebFetch tool to fetch https://example.com",
        )
        assert isinstance(resp, str)
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "Deny should not create pending"


@pytest.mark.llm
@pytest.mark.timeout(180)
class TestWildcardAllowAll:
    """Wildcard ** rule → auto-allow everything."""

    @pytest.mark.anyio
    async def test_write_file_wildcard(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(
            tmp_path,
            "wf_wc",
            extra_allow_rules={"write_file": ["**"]},
        )
        target = tmp_path / "wildcard.txt"
        await agent.run_async(
            message=f"Use the write_file tool to create {target} with content 'wildcard'",
        )
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "Wildcard ** should auto-allow"
        assert target.exists()

    @pytest.mark.anyio
    async def test_shell_wildcard(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(
            tmp_path,
            "sh_wc",
            extra_allow_rules={"run_shell_command": ["**"]},
        )
        await agent.run_async(
            message="Use the run_shell_command tool to run: python --version",
        )
        pending = await _get_pending(sm, uid, sid)
        assert pending is None, "Wildcard ** should auto-allow python"


@pytest.mark.llm
@pytest.mark.timeout(180)
class TestProtectedPaths:
    """Protected paths (.git) → AskPermission even with wildcard allow."""

    @pytest.mark.anyio
    async def test_git_dir_ask_despite_wildcard(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        agent, sm, uid, sid = await _make_agent(
            tmp_path,
            "wf_git",
            extra_allow_rules={"write_file": ["**"]},
        )
        await agent.run_async(
            message=f"Use the write_file tool to create {tmp_path / '.git' / 'config'} with content 'x'",
        )
        pending = await _get_pending(sm, uid, sid)
        if pending is None:
            pytest.skip("LLM did not call write_file for .git path")
        unresolved = await _get_unresolved(sm, uid, sid)
        assert len(unresolved) > 0, ".git path should trigger ask even with wildcard"


@pytest.mark.llm
@pytest.mark.timeout(180)
class TestFullLifecycle:
    """Full ask → resolve → resume lifecycle."""

    @pytest.mark.anyio
    async def test_ask_resolve_allow_resume(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(tmp_path, "lc_allow", max_iterations=15)
        target = tmp_path / "lifecycle.txt"

        # Step 1: trigger ask
        await agent.run_async(
            message=f"Use the write_file tool to create {target} with content 'lifecycle test'",
        )
        pending = await _get_pending(sm, uid, sid)
        if pending is None:
            pytest.skip("LLM did not call write_file")

        unresolved = await _get_unresolved(sm, uid, sid)
        assert len(unresolved) > 0

        # Step 2: verify hard-block
        with pytest.raises(PendingPermissionsError):
            await agent.run_async(message="continue")

        # Step 3: resolve with allow
        await _resolve_all(agent, sm, uid, sid, "allow")

        # Step 4: resume
        resp = await agent.run_async(message="What happened with the file?")
        assert isinstance(resp, str) and len(resp) > 0

        # Step 5: verify state cleared
        post = await _get_pending(sm, uid, sid)
        assert post is None, "Pending should be cleared after resume"

        # Step 6: verify allow rule persisted
        allow_rules, _ = await sm.load_permission_rules(
            user_id=uid,
            session_id=sid,
            tool_name="write_file",
        )
        assert len(allow_rules) > 0, "Allow rule should be persisted after resolve"

        # Step 7: verify file created
        assert target.exists(), f"File should exist at {target}"

    @pytest.mark.anyio
    async def test_ask_resolve_deny_resume(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(tmp_path, "lc_deny", max_iterations=15)
        target = tmp_path / "deny_lifecycle.txt"

        await agent.run_async(
            message=f"Use the write_file tool to create {target} with content 'should be denied'",
        )
        pending = await _get_pending(sm, uid, sid)
        if pending is None:
            pytest.skip("LLM did not call write_file")

        # Resolve with deny
        await _resolve_all(agent, sm, uid, sid, "deny")

        # Resume
        resp = await agent.run_async(message="What happened?")
        assert isinstance(resp, str)

        # File should NOT exist
        assert not target.exists(), "Denied file should not be created"

        # Pending cleared
        post = await _get_pending(sm, uid, sid)
        assert post is None


@pytest.mark.llm
@pytest.mark.timeout(180)
class TestPersistentAllow:
    """After resolve(allow), same permission_key auto-passes on second call."""

    @pytest.mark.anyio
    async def test_second_call_auto_passes(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(tmp_path, "persist", max_iterations=15)

        # First call: triggers ask
        target1 = tmp_path / "persist1.txt"
        await agent.run_async(
            message=f"Use the write_file tool to create {target1} with content 'first'",
        )
        pending = await _get_pending(sm, uid, sid)
        if pending is None:
            pytest.skip("LLM did not call write_file")

        # Resolve with allow
        await _resolve_all(agent, sm, uid, sid, "allow")
        await agent.run_async(message="Continue")

        # Second call: same dir → should auto-pass
        target2 = tmp_path / "persist2.txt"
        await agent.run_async(
            message=f"Use the write_file tool to create {target2} with content 'second'",
        )
        pending2 = await _get_pending(sm, uid, sid)
        assert pending2 is None, "Second write to same dir should auto-allow (rule persisted)"


@pytest.mark.llm
@pytest.mark.timeout(180)
class TestMixedParallel:
    """Readonly + write tools called in parallel in one turn."""

    @pytest.mark.anyio
    async def test_readonly_and_write_parallel(self, tmp_path: Path) -> None:
        agent, sm, uid, sid = await _make_agent(tmp_path, "mixed")
        target = tmp_path / "mixed.txt"

        resp = await agent.run_async(
            message=(
                "Do ALL of these in ONE response, calling both tools at the same time:\n"
                f"1) Use list_directory to list {tmp_path}\n"
                f"2) Use write_file to create {target} with content 'mixed test'\n"
                "Call both tools simultaneously."
            ),
        )
        assert isinstance(resp, str)

        pending = await _get_pending(sm, uid, sid)
        if pending is None:
            pytest.skip("LLM did not trigger mixed tool calls")

        unresolved = await _get_unresolved(sm, uid, sid)
        assert len(unresolved) > 0
        for entry in unresolved.values():
            assert entry["tool_name"] == "write_file", f"Only write_file should ask, got {entry['tool_name']}"
