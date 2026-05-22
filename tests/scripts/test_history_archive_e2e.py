# Copyright (c) Nex-AGI. All rights reserved.
# Licensed under the Apache License, Version 2.0
"""RFC-0021 端到端真实测试: 多轮对话强制触发 llm_summary 压缩 + 归档 + 召回。

策略:
  - SessionManager + InMemoryDatabaseEngine 让多次 agent.run() 共享历史
  - 每次 agent.run() = 1 个 USER iteration
  - llm_summary 策略 + keep_iterations=2: 第 3 个 turn 起就会压缩前面的轮次
  - 最后一轮要求 agent 回忆早期细节, 期望它按压缩 hint 去临时归档目录找

运行:
    cd /Users/yiran/Projects/nexau-compact-save-history
    LANGFUSE_SECRET_KEY=... LANGFUSE_PUBLIC_KEY=... LANGFUSE_HOST=... \\
    LLM_MODEL=... LLM_BASE_URL=... LLM_API_KEY=... \\
    uv run python tests/scripts/test_history_archive_e2e.py
"""

import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from nexau import Agent, AgentConfig
from nexau.archs.main_sub.execution.middleware.agent_events_middleware import AgentEventsMiddleware, Event
from nexau.archs.main_sub.execution.middleware.context_compaction import ContextCompactionMiddleware
from nexau.archs.session import SessionManager
from nexau.archs.session.orm.memory_engine import InMemoryDatabaseEngine


def _make_langfuse_tracer(session_id: str) -> Any:
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
    sk = os.environ.get("LANGFUSE_SECRET_KEY")
    if not pk or not sk:
        return None
    from nexau.archs.tracer.adapters.langfuse import LangfuseTracer

    host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
    return LangfuseTracer(public_key=pk, secret_key=sk, host=host, debug=False, session_id=session_id)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def get_date():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_event_counter = {"text": 0, "tool_call": 0, "tool_result": 0, "compaction": 0}


def handler(event: Event) -> None:
    et = type(event).__name__
    if "Compaction" in et:
        _event_counter["compaction"] += 1
        print(f"\n>>> {et}\n")
    elif "ToolCallStart" in et:
        _event_counter["tool_call"] += 1


def main() -> int:
    project_root = Path(__file__).parent.parent.parent

    # 0. sandbox.work_dir 用独立 tempdir 隔离 (不再借用项目根目录).
    #    归档本身写到 sandbox temp_dir；这里仍隔离工作目录以避免工具输出污染项目树。
    sandbox_dir = Path(tempfile.mkdtemp(prefix="rfc0021-e2e-"))
    os.environ["SANDBOX_WORK_DIR"] = str(sandbox_dir)

    # 1. 加载 code_agent base config, flatten
    code_agent_dir = project_root / "examples" / "code_agent"
    config = AgentConfig.from_yaml(config_path=code_agent_dir / "code_agent.yaml")
    config.sub_agents = None
    config.max_context_tokens = 100000  # 真实尺寸: threshold 0.65 → ~65K 触发
    config.max_iterations = 250  # 重负载多轮 + 大量工具调用, 给召回足够空间

    # 2. 接入 ContextCompactionMiddleware
    compaction_mw = ContextCompactionMiddleware(
        max_context_tokens=100000,
        auto_compact=True,
        threshold=0.65,
        compaction_strategy="llm_summary",
        keep_iterations=3,
        # RFC-0021
        save_history=True,
    )
    if config.middlewares is None:
        config.middlewares = []
    config.middlewares.append(compaction_mw)

    session_id = f"compact-preserve-user-{int(time.time())}"
    event_mw = AgentEventsMiddleware(session_id=session_id, on_event=handler)
    config.middlewares.append(event_mw)

    # Langfuse tracing (uploads compaction + LLM + tool spans)
    tracer = _make_langfuse_tracer(session_id)
    if tracer is not None:
        config.tracers = [tracer]
        print(f"Langfuse tracing enabled (session_id={session_id})")
    else:
        print("Langfuse tracing DISABLED (LANGFUSE_PUBLIC_KEY/SECRET_KEY missing)")

    # 3. SessionManager 让多次 agent.run() 共享历史
    engine = InMemoryDatabaseEngine()
    session_manager = SessionManager(engine=engine)

    agent = Agent(
        config=config,
        session_manager=session_manager,
        user_id="compact-preserve-tester",
        session_id=session_id,
    )
    sandbox = agent.sandbox_manager.instance
    if sandbox is None:
        print("✗ Sandbox failed to start", file=sys.stderr)
        return 1
    sandbox_root = Path(str(sandbox.work_dir))
    print(f"Sandbox work_dir: {sandbox_root}")
    print(f"Session id:        {session_id}\n")

    # 4. 三个独立复杂任务 (全部 read-only, 避免 write 权限卡住):
    #    Task A: RFC 与实现漂移审计
    #    Task B: 错误恢复路径完整追踪
    #    Task C: 压缩策略决策树 + 跨任务 recall
    nexau_root = f"{project_root}/nexau"
    mw_dir = f"{nexau_root}/archs/main_sub/execution/middleware"
    cc_dir = f"{mw_dir}/context_compaction"
    exec_dir = f"{nexau_root}/archs/main_sub/execution"
    rfc_dir = f"{project_root}/docs/rfcs"

    turns = [
        # ─── Task A: RFC 与实现漂移审计 ────────────────────────────────────────
        (
            "A1",
            f"You will conduct a design-vs-implementation audit. First, read the RFC document with "
            f"read_file (limit=600, offset=0): {rfc_dir}/0021-history-archive-on-compaction.md\n\n"
            f"Then summarize in 6-8 bullets what the RFC promises: archive layout, file naming, "
            f"hint injection behavior, opt-in/opt-out switches. Quote the exact promise text where useful.",
        ),
        (
            "A2",
            f"Now read the implementation files (limit=600 each, offset=0):\n"
            f"  1. {cc_dir}/history_archive.py\n"
            f"  2. {cc_dir}/middleware.py\n\n"
            f"Map each RFC promise from A1 to the concrete function / class / constant that implements it. "
            f"Use a markdown table with columns: RFC promise | Implementation location (file:lineno) | Notes.",
        ),
        (
            "A3",
            f"Re-read the config schema (limit=400, offset=0): {cc_dir}/config.py\n\n"
            f"Find every config field related to archiving, then cross-reference against A1's promises. "
            f"List any config field that is NOT mentioned in the RFC, AND any RFC promise that has NO "
            f"matching config field. These are 'drift candidates'.",
        ),
        (
            "A4",
            f"Read the test for history archive (limit=500, offset=0): "
            f"{project_root}/tests/unit/test_history_archive.py\n\n"
            f"For each drift candidate identified in A3, check whether tests exist that validate the "
            f"actual (post-drift) behavior. List drift items that ARE tested vs NOT tested.",
        ),
        (
            "A5-SYNTHESIS",
            "Produce a Task-A final report (in your reply, no file writes): a numbered list of every "
            "concrete drift between RFC-0021 and the implementation, with severity (low / medium / high) "
            "and one-sentence recommendation. End with: 'Task A complete.'",
        ),
        # ─── Task B: 错误恢复路径完整追踪 ──────────────────────────────────────
        (
            "B1",
            f"Switching tasks. New task: trace what happens when an LLM provider returns a 5xx error. "
            f"Read the RFC first (limit=500, offset=0): {rfc_dir}/0003-llm-failover-middleware.md\n\n"
            f"Summarize the failover state machine in 5-6 bullets: trigger conditions, fallback ordering, "
            f"circuit breaker states, what gets preserved across fallback (tools, tool_choice, max_tokens).",
        ),
        (
            "B2",
            f"Read the failover middleware implementation (limit=600, offset=0): "
            f"{mw_dir}/llm_failover.py\n\n"
            f"For each state transition in the state machine you described in B1, cite the exact method "
            f"and line range that implements it. Format: 'CLOSED → OPEN: handled by `_method_name` at "
            f"line X-Y, condition: ...'",
        ),
        (
            "B3",
            f"Read the LLM caller and the RFC for emergency compaction (limit=500 each, offset=0):\n"
            f"  1. {exec_dir}/llm_caller.py\n"
            f"  2. {rfc_dir}/0004-context-overflow-emergency-compaction-and-error-events.md\n\n"
            f"Explain how a `prompt_too_long` 4xx error is detected, AND how it differs from a 5xx error "
            f"in terms of recovery path. Cite the relevant exception classes and the marker strings used "
            f"for detection.",
        ),
        (
            "B4",
            f"Read the executor and the compaction middleware's wrap_model_call (limit=500 each, "
            f"offset=0):\n"
            f"  1. {exec_dir}/executor.py\n"
            f"  2. {cc_dir}/middleware.py\n\n"
            f"Now trace the FULL recovery chain for a 5xx error in the middle of an agent loop: "
            f"which middleware sees it first, which retries / which falls through, how state is rolled "
            f"back, and whether emergency compaction can be triggered as a side effect.",
        ),
        (
            "B5-SYNTHESIS",
            "Produce a Task-B final report (in your reply): a single ASCII flow diagram covering both "
            "the 5xx failover path AND the prompt-too-long emergency compaction path, with branching "
            "from a common 'Provider error caught' node. Label each branch with the exception class "
            "and the responsible middleware. End with: 'Task B complete.'",
        ),
        # ─── Task C: 压缩策略决策树 + 跨任务 recall ──────────────────────────
        (
            "C1",
            f"Switching tasks. New task: produce a decision tree for choosing a compact strategy. "
            f"Read the THREE strategies (limit=600 each, offset=0):\n"
            f"  1. {cc_dir}/compact_stratigies/sliding_window.py\n"
            f"  2. {cc_dir}/compact_stratigies/compact_tool_result.py\n"
            f"  3. {cc_dir}/compact_stratigies/user_model_full_trace_adaptive.py\n\n"
            f"For each strategy, summarize: what is preserved verbatim, what is collapsed, the input "
            f"size guard-rails it has (chunking? truncation?), and the failure-fallback path.",
        ),
        (
            "C2",
            f"Read the config + factory to see how a strategy is selected (limit=400 each, offset=0):\n"
            f"  1. {cc_dir}/config.py\n"
            f"  2. {cc_dir}/factory.py\n\n"
            f"Document the config knobs that gate strategy selection: enum values, conflicting flags, "
            f"and any auto-deprecation aliases (e.g. legacy names mapped to new ones).",
        ),
        (
            "C3",
            f"Read the trigger strategies (limit=300 each, offset=0):\n"
            f"  1. {cc_dir}/trigger_strategies/token_threshold.py\n"
            f"  2. {cc_dir}/trigger_strategies/time_based.py\n\n"
            f"Pair each trigger with the compact strategies that make sense for it. Example: "
            f"'time_based + tool_result_compaction → micro-compact pattern for long tool outputs.' "
            f"Give 3-4 such pairings with rationale.",
        ),
        (
            "C4-DECISION-TREE",
            "Now produce a decision tree (ASCII or nested markdown lists) that walks a user through: "
            "'Q1: are user messages critical to preserve? Q2: are tool outputs dominating context? "
            "Q3: is response latency bounded?' — each leaf points to a (trigger, strategy) pair with "
            "an explanation. Aim for at least 6 leaves.",
        ),
        (
            "C5-RECALL",
            "Final cross-task recall task. You worked on Task A (RFC drift audit), Task B (error "
            "recovery), and Task C (strategy decision tree). Some early turns are now outside your "
            "active context due to compaction. "
            "**Use search_file_content (grep) on the archive directory from the compaction hint** —— each "
            'line is either a serialized Message JSON or a boundary record `{"_boundary": ...}` —— '
            "to recover three specific facts: "
            "(1) the RFC promise(s) that A3 marked as 'drift candidates'; "
            "(2) the exact exception class name(s) used to detect prompt_too_long errors from B3; "
            "(3) the legacy compaction_strategy alias mentioned in C2 that's auto-mapped to a new name. "
            "Cite the transcript line excerpts (or boundary previews) where you found each. "
            "Then call complete_task with the three facts and your sources.",
        ),
    ]

    for tag, msg in turns:
        print("\n" + "=" * 70)
        print(f"### Turn {tag}")
        print("=" * 70)
        print(f"USER: {msg[:200]}{'…' if len(msg) > 200 else ''}\n")
        try:
            response = agent.run(
                message=msg,
                context={
                    "date": get_date(),
                    "username": os.getenv("USER"),
                    "working_directory": str(sandbox_root),
                    "env_content": {
                        "date": get_date(),
                        "username": os.getenv("USER"),
                        "working_directory": str(sandbox_root),
                    },
                },
            )
            text = response if isinstance(response, str) else str(response)
            print(f"\nASSISTANT [{tag}]: {text[:1500]}")
        except Exception as exc:
            print(f"\n✗ Turn {tag} failed: {exc}", file=sys.stderr)
            import traceback

            traceback.print_exc()
            break

    # 5. 检查归档
    archive_writer = getattr(compaction_mw, "_archive_writer", None)
    if archive_writer is None:
        print("\n✗ archive writer was not initialized — no compaction archive can be inspected", file=sys.stderr)
        return 1
    archive_dir = Path(archive_writer.archive_dir)
    print("\n" + "=" * 70)
    print("RFC-0021 Archive Inspection")
    print("=" * 70)
    print(f"Archive dir:        {archive_dir}")
    print(f"Exists:             {archive_dir.exists()}")
    print(f"Compaction events:  {_event_counter['compaction']}")
    print(f"Tool calls:         {_event_counter['tool_call']}")

    if not archive_dir.exists():
        print("\n✗ 归档目录未创建 — 没触发实际移除", file=sys.stderr)
        return 1

    files = sorted(archive_dir.iterdir())
    print(f"\nFiles ({len(files)}):")
    for f in files:
        print(f"  {f.name:30s}  {f.stat().st_size:>8d} bytes")

    # RFC-0021 单文件: transcript.jsonl 内每行是 Message JSON 或 boundary 记录
    transcript_path = archive_dir / "transcript.jsonl"
    if not transcript_path.exists():
        print("\n✗ transcript.jsonl 不存在 — 没触发实际压缩或归档关闭", file=sys.stderr)
        return 1

    boundaries: list[dict[str, Any]] = []
    archived_msg_lines: list[str] = []
    for raw in transcript_path.read_text(encoding="utf-8").splitlines():
        ln = raw.strip()
        if not ln:
            continue
        obj = json.loads(ln)
        if isinstance(obj, dict) and "_boundary" in obj:
            obj_dict = cast("dict[str, Any]", obj)
            b_val = obj_dict["_boundary"]
            if isinstance(b_val, dict):
                boundaries.append(cast("dict[str, Any]", b_val))
        else:
            archived_msg_lines.append(ln)

    print(f"\ntranscript.jsonl: {len(archived_msg_lines)} archived messages, {len(boundaries)} boundary records")
    for b in boundaries:
        print(
            f"  Round {b['round']:2d}: "
            f"{b['removed_message_count']:>3d} msgs removed, "
            f"tokens {b.get('tokens_before')} -> {b.get('tokens_after')}, "
            f"strategy={b['strategy']}, "
            f"trigger={b['trigger_reason']}"
        )
        preview = (b.get("preview") or "")[:120]
        if preview:
            print(f"          preview: {preview!r}")

    print("\n" + "=" * 70)
    print("Invariants check")
    print("=" * 70)

    def check(label: str, ok: bool, detail: str = "") -> None:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {label}{(' — ' + detail) if detail else ''}")

    check("至少触发 1 轮压缩", len(boundaries) >= 1, f"rounds={len(boundaries)}")
    if len(boundaries) >= 2:
        check("✨ 多轮压缩被覆盖 (RFC-0021 关键场景)", True, f"rounds={len(boundaries)}")

    # 每行 Message JSON 都能被 Pydantic 反序列化
    from nexau.core.messages import Message

    parseable = True
    for ln in archived_msg_lines:
        try:
            Message.model_validate_json(ln)
        except Exception as exc:
            parseable = False
            print(f"  ✗ Message 反序列化失败: {exc}")
            break
    check(f"全部 {len(archived_msg_lines)} 条归档 Message 都可反序列化", parseable)

    # round 数 = boundary 行数;  removed_count 累计 = 实际归档消息数
    expected_total = sum(b.get("removed_message_count", 0) for b in boundaries)
    check(
        "Σ removed_message_count == 实际归档消息行数",
        expected_total == len(archived_msg_lines),
        f"sum={expected_total}, actual={len(archived_msg_lines)}",
    )

    # Turn 1 内容应在 transcript 中
    found_t1 = "agent_events_middleware" in transcript_path.read_text(encoding="utf-8")
    check("Turn 1 (agent_events_middleware) 内容仍可被 grep 到", found_t1)

    # === 新 compact_prompt 验证: summary 必须逐字保留所有 user message ===
    print("\n" + "=" * 70)
    print("User-message preservation check (new compact_prompt)")
    print("=" * 70)

    summary_msgs = [
        m for m in agent.history if (m.metadata or {}).get("isSummary") is True or (m.metadata or {}).get("is_compacted") is True
    ]
    print(f"Found {len(summary_msgs)} summary message(s) in current in-memory context")

    if not summary_msgs:
        print("⚠️  内存里没有 summary message —— 可能压缩还没发生，或全部摘要都被后续压缩吃掉")
    else:
        # 15 条 user turn 的关键 verbatim 子串 (每条独特、不会跨 turn 重叠)
        # 子串选取原则: 必须在该 turn 的 USER prompt 文本里出现, 且不会出现在其他 turn 的 prompt 里
        must_appear = [
            # Task A
            ("A1", "Quote the exact promise text where useful"),
            ("A2", "RFC promise | Implementation location (file:lineno)"),
            ("A3", "drift candidates"),
            ("A4", "test_history_archive.py"),
            ("A5-SYNTHESIS", "'Task A complete.'"),
            # Task B
            ("B1", "0003-llm-failover-middleware.md"),
            ("B2", "exact method and line range"),
            ("B3", "0004-context-overflow-emergency-compaction"),
            ("B4", "wrap_model_call"),
            ("B5-SYNTHESIS", "'Task B complete.'"),
            # Task C
            ("C1", "user_model_full_trace_adaptive.py"),
            ("C2", "auto-deprecation aliases"),
            ("C3", "trigger_strategies/token_threshold.py"),
            ("C4-DECISION-TREE", "Q1: are user messages critical to preserve?"),
            ("C5-RECALL", "Use search_file_content (grep) on the archive directory from the compaction hint"),
        ]
        summary_text = "\n\n".join(m.get_text_content() for m in summary_msgs)
        print(f"Summary text length: {len(summary_text)} chars")

        # 分类: 仍 live 的 user message (在 agent.history 里作为 Role.USER 原样存在) vs 已被压缩
        from nexau.core.messages import Role

        live_user_text_blob = "\n\n".join(
            m.get_text_content() for m in agent.history if m.role == Role.USER and not (m.metadata or {}).get("isSummary")
        )

        compacted_pass = 0
        compacted_fail = 0
        live_count = 0
        for tag, needle in must_appear:
            if needle in live_user_text_blob:
                # 这条 user message 还活着, 不需要进 summary
                check(f"{tag} 仍 live 在 agent.history (跳过 summary 检查)", True, repr(needle))
                live_count += 1
            else:
                # 这条 user message 应该已被压缩, 必须出现在 summary 里 verbatim
                ok = needle in summary_text
                check(f"{tag} 已压缩 → 必须 verbatim 出现在 summary", ok, repr(needle))
                if ok:
                    compacted_pass += 1
                else:
                    compacted_fail += 1

        # Prompt 落地结构: section 标题 / 编号引用
        section_present = "All User Messages" in summary_text or "user turn" in summary_text.lower()
        check("Summary 含 'All User Messages' / 'user turn N' 结构", section_present)

        print(
            f"\nSummary 保留统计: 被压缩并 verbatim 保留 = {compacted_pass}, "
            f"被压缩但 summary 中缺失 = {compacted_fail}, 仍 live = {live_count}"
        )

        # 打印 summary 节选便于人工核查
        print("\n--- Summary text excerpt (first 8000 chars) ---")
        print(summary_text[:8000])
        print("--- end excerpt ---")

    # Flush langfuse 让 trace 立即出现在 UI
    if tracer is not None:
        print("\nFlushing Langfuse tracer...")
        tracer.flush()
        print(f"Langfuse host: {os.environ.get('LANGFUSE_HOST')}")
        print(f"Filter by session_id={session_id}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
