"""Interactive demo for CC-aligned agent with RFC-0019 permissions.

Usage:
    # Local mode (default): runs tools on your local machine
    uv run python scripts/demo_cc_agent.py

    # E2B mode: runs file/shell tools in remote sandbox
    export E2B_API_URL="https://hk-prod-e2b.xiaobei.top"
    export E2B_API_KEY="your_key"
    export E2B_DOMAIN="hk-prod-e2b.xiaobei.top"
    uv run python scripts/demo_cc_agent.py --e2b

Full tool list (19 YAML + 3 framework-auto, CC-aligned permissions):
  - Readonly (auto-allow): read_file, read_many_files, read_visual_file,
    list_directory, glob, search_file_content, web_search
  - File write (path-level): write_file, replace, apply_patch, multiedit_tool
  - Shell (readonly whitelist + command-level): run_shell_command
  - Code execution (every call ask): run_code_tool
  - Web fetch (domain-level): web_fetch
  - Session (auto-allow): save_memory, write_todos, complete_task, ask_user

Test plan: docs/testing/permission-e2e-test-plan.md
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.permissions.types import PendingPermissionsError
from nexau.archs.sandbox.base_sandbox import E2BSandboxConfig, LocalSandboxConfig
from nexau.archs.session import SessionManager
from nexau.archs.session.orm import InMemoryDatabaseEngine
from nexau.archs.tool.tool import Tool
from nexau.archs.tracer.adapters.langfuse import LangfuseTracer

load_dotenv()

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOOLS_DIR = Path(__file__).resolve().parent.parent / "examples" / "cc_agent" / "tools"

SYSTEM_PROMPT = """\
You are a coding assistant with access to a development environment.

RULES:
- When asked to do multiple things, call ALL tools in ONE response.
- If a tool call fails due to permission, report it and continue.
- Do NOT ask for confirmation — just call tools directly.
- Keep responses concise. Reply in Chinese.

Working directory: {work_dir}
"""

# ---------------------------------------------------------------------------
# Sandbox config
# ---------------------------------------------------------------------------


def _build_sandbox_config(use_e2b: bool) -> LocalSandboxConfig | E2BSandboxConfig:
    if not use_e2b:
        work_dir = os.getenv("SANDBOX_WORK_DIR", os.getcwd())
        return LocalSandboxConfig(work_dir=work_dir)

    api_key = os.getenv("E2B_API_KEY")
    if not api_key:
        print("ERROR: E2B_API_KEY not set.")
        print("  export E2B_API_URL='https://hk-prod-e2b.xiaobei.top'")
        print("  export E2B_API_KEY='your_key'")
        print("  export E2B_DOMAIN='hk-prod-e2b.xiaobei.top'")
        sys.exit(1)

    return E2BSandboxConfig(
        type="e2b",
        api_key=api_key,
        api_url=os.getenv("E2B_API_URL") or None,
        template=os.getenv("E2B_TEMPLATE", "test"),
        timeout=int(os.getenv("E2B_TIMEOUT", "300")),
        work_dir=os.getenv("E2B_WORK_DIR", "/home/user"),
        metadata={"example": "cc_agent", "launcher": "demo_cc_agent.py"},
    )


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def _build_tools() -> list[Tool]:
    """Build all CC-aligned tools with permission configurations."""
    from nexau.archs.tool.builtin import background_task_manage_tool
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
    from nexau.archs.tool.builtin.session_tools import (
        ask_user,
        complete_task,
        save_memory,
        write_todos,
    )
    from nexau.archs.tool.builtin.shell_tools import run_shell_command
    from nexau.archs.tool.builtin.web_tools import google_web_search, web_fetch

    tools: list[Tool] = []

    # ── Readonly tools: no permissions → default allow_rules=["**"] ──

    tools.append(Tool.from_yaml(str(TOOLS_DIR / "read_file.tool.yaml"), binding=read_file))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "read_many_files.tool.yaml"), binding=read_many_files))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "read_visual_file.tool.yaml"), binding=read_visual_file))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "glob.tool.yaml"), binding=glob))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "list_directory.tool.yaml"), binding=list_directory))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "search_file_content.tool.yaml"), binding=search_file_content))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "WebSearch.tool.yaml"), binding=google_web_search))

    # ── File write tools: path-level allow / deny ──

    empty_permissions: dict[str, list[str]] = {"allow": [], "deny": []}

    tools.append(Tool.from_yaml(str(TOOLS_DIR / "write_file.tool.yaml"), binding=write_file, permissions=empty_permissions))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "replace.tool.yaml"), binding=replace, permissions=empty_permissions))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "apply_patch.tool.yaml"), binding=apply_patch, permissions=empty_permissions))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "multiedit_tool.tool.yaml"), binding=multiedit_tool, permissions=empty_permissions))

    # ── Shell: readonly whitelist auto-allow, all else ask ──

    tools.append(
        Tool.from_yaml(
            str(TOOLS_DIR / "run_shell_command.tool.yaml"),
            binding=run_shell_command,
            permissions=empty_permissions,
        )
    )

    # ── Code execution: every call ask ──

    tools.append(
        Tool.from_yaml(
            str(TOOLS_DIR / "run_code_tool.tool.yaml"),
            binding=run_code_tool,
            permissions={"allow": [], "deny": []},
        )
    )

    # ── Web fetch: domain-level, start empty (every domain asks) ──

    tools.append(
        Tool.from_yaml(
            str(TOOLS_DIR / "WebFetch.tool.yaml"),
            binding=web_fetch,
            permissions={"allow": [], "deny": []},
        )
    )

    # ── Shell helper: no permissions → auto-allow ──

    tools.append(
        Tool.from_yaml(
            str(TOOLS_DIR / "BackgroundTaskManage.tool.yaml"),
            binding=background_task_manage_tool,
        )
    )

    # ── Session tools: no permissions → auto-allow ──

    tools.append(Tool.from_yaml(str(TOOLS_DIR / "save_memory.tool.yaml"), binding=save_memory))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "write_todos.tool.yaml"), binding=write_todos))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "complete_task.tool.yaml"), binding=complete_task))
    tools.append(Tool.from_yaml(str(TOOLS_DIR / "ask_user.tool.yaml"), binding=ask_user))

    # NOTE: sub_agent (call_sub_agent), tool_search, skill_tool
    # 由框架根据 AgentConfig 自动注册。

    return tools


# ---------------------------------------------------------------------------
# Interactive loop
# ---------------------------------------------------------------------------


def _build_mcp_servers(work_dir: str) -> list[dict[str, object]]:
    """Build MCP server configs (CC-aligned: server 级 always-ask)."""
    return [
        {
            "name": "filesystem",
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", work_dir],
            "timeout": 30,
            "permissions": {"allow": [], "deny": []},
        },
    ]


async def main() -> None:
    use_e2b = "--e2b" in sys.argv
    use_mcp = "--mcp" in sys.argv
    sandbox_config = _build_sandbox_config(use_e2b)

    engine = InMemoryDatabaseEngine()
    sm = SessionManager(engine=engine)
    await sm.setup_models()

    tools = _build_tools()
    mcp_servers = _build_mcp_servers(sandbox_config.work_dir) if use_mcp else []
    user_id = "test_user"
    session_id = "cc_agent_test"

    await sm.init_permission_rules_from_config(
        user_id=user_id,
        session_id=session_id,
        tools=tools,
    )

    config = AgentConfig(
        name="cc_agent_permission_test",
        system_prompt=SYSTEM_PROMPT.format(work_dir=sandbox_config.work_dir),
        llm_config=LLMConfig(temperature=0),
        tools=tools,
        mcp_servers=mcp_servers,
        max_iterations=15,
        sandbox_config=sandbox_config,
        tracers=[LangfuseTracer()],
    )

    agent = await Agent.create(
        config=config,
        session_manager=sm,
        user_id=user_id,
        session_id=session_id,
    )

    mode = "E2B sandbox" if use_e2b else "Local"
    print(f"Mode: {mode}")
    print(f"Working directory: {sandbox_config.work_dir}")

    # ── Helper: check and resolve pending permissions ──

    async def _check_and_resolve_pending() -> bool:
        pending = await sm.get_pending_tool_calls(
            user_id=user_id,
            session_id=session_id,
        )
        if not pending:
            return False

        unresolved = {k: v for k, v in pending.items() if v.get("decision") is None}
        if not unresolved:
            return False

        print()
        print("\033[31m⚠ Permission required!\033[0m")
        print("-" * 50)

        for tc_id, entry in unresolved.items():
            print(f"  Tool:   {entry['tool_name']}")
            print(f"  Prompt: {entry['prompt']}")
            print(f"  ID:     {tc_id}")
            print()

            while True:
                choice = input("  → allow / allow_once / deny: ").strip().lower()
                if choice in ("allow", "allow_once", "deny"):
                    break
                print("    (please type: allow, allow_once, or deny)")

            await agent.resolve_permission(tc_id, choice)
            print(f"  ✓ Resolved: {choice}")
            print()

        print("-" * 50)
        print("Permissions resolved. Resuming...")
        print()
        return True

    # ── Print header ──

    # ── Discover MCP tools (registered during Agent.create) ──
    mcp_tool_names = [t.name for t in agent._tool_registry.compute_eager_tools() if t.name.startswith("mcp__")]

    print()
    print("=" * 60)
    print(f"CC-Aligned Agent — Full Permission Test ({mode})")
    print("=" * 60)
    print()
    yaml_count = len(tools)
    mcp_count = len(mcp_tool_names)
    print(f"Tools ({yaml_count} YAML + {mcp_count} MCP + 3 framework-auto):")
    print("  Readonly (auto-allow):  read_file, read_many_files, read_visual_file,")
    print("                          glob, list_directory, search_file_content, web_search")
    print("  File write (all ask):   write_file, replace, apply_patch, multiedit_tool")
    print("  Shell (whitelist+ask):  run_shell_command  (readonly cmds auto, rest ask)")
    print("  Shell helper:           BackgroundTaskManage")
    print("  Code exec (always ask): run_code_tool")
    print("  Web fetch (domain ask): web_fetch")
    print("  Session (auto-allow):   save_memory, write_todos, complete_task, ask_user")
    print("  Framework-auto:         sub_agent (explore), tool_search, skill_tool")
    if mcp_tool_names:
        print(f"  MCP (always ask):       {', '.join(mcp_tool_names)}")
    print()
    print("CC-aligned: no hardcoded deny — user decides via allow/deny responses.")
    work_dir = sandbox_config.work_dir
    print()
    print("Suggested tests:")
    print(f"  1. 列出 {work_dir} 目录                        (readonly → auto)")
    print(f"  2. 创建 {work_dir}/hello.py 写 print('hi')    (write → ask)")
    print("  3. 用 run_code_tool 执行 print(1+1)           (code → ask)")
    print(f"  4. 执行 ls -la {work_dir}                      (shell readonly → auto)")
    print(f"  5. 执行 rm {work_dir}/hello.py                 (shell → ask)")
    print("  6. 执行 python hello.py                        (shell → ask)")
    print("  7. 抓取 https://example.com                    (web → ask)")
    print("  8. 同时: 读文件 + 写文件 + rm 文件             (parallel mixed)")
    print()
    print("Type 'quit' to exit.")
    print("=" * 60)

    # ── Main loop ──

    while True:
        print()
        user_input = input("\033[36mYou: \033[0m").strip()
        if not user_input or user_input.lower() in ("quit", "exit", "q"):
            print("Bye!")
            break

        try:
            response = await agent.run_async(message=user_input)
            print(f"\033[33mAgent:\033[0m {response}")

            if await _check_and_resolve_pending():
                response = await agent.run_async(
                    message="Continue with the results from the operations.",
                )
                print(f"\033[33mAgent:\033[0m {response}")

        except PendingPermissionsError:
            if await _check_and_resolve_pending():
                response = await agent.run_async(
                    message="Continue with the results from the operations.",
                )
                print(f"\033[33mAgent:\033[0m {response}")

        except KeyboardInterrupt:
            print("\nBye!")
            break
        except Exception as e:
            print(f"\033[31mError: {e}\033[0m")
            import traceback

            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
