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

"""Tests for SensitiveWordMiddleware."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexau.archs.main_sub.execution.hooks import (
    AfterModelHookInput,
    BeforeModelHookInput,
)
from nexau.archs.main_sub.execution.middleware.sensitive_word import (
    SensitiveContentBlockedError,
    SensitiveWordMiddleware,
    _AhoCorasick,  # pyright: ignore[reportPrivateUsage]
)
from nexau.archs.main_sub.execution.stop_reason import AgentStopReason
from nexau.core.messages import Message, Role, TextBlock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _before_input(messages: list[Message]) -> BeforeModelHookInput:
    """Build a minimal BeforeModelHookInput for input-side tests."""
    return BeforeModelHookInput(
        agent_state=MagicMock(),
        max_iterations=10,
        current_iteration=0,
        messages=messages,
    )


def _after_output(messages: list[Message], original_response: str) -> AfterModelHookInput:
    """Build a minimal AfterModelHookInput for output-side tests."""
    return AfterModelHookInput(
        agent_state=MagicMock(),
        max_iterations=10,
        current_iteration=0,
        messages=messages,
        original_response=original_response,
    )


def _user(text: str) -> Message:
    return Message(role=Role.USER, content=[TextBlock(text=text)])


def _assistant(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=[TextBlock(text=text)])


# ---------------------------------------------------------------------------
# _AhoCorasick
# ---------------------------------------------------------------------------


class TestAhoCorasick:
    def test_basic_single_match(self) -> None:
        ac = _AhoCorasick()
        ac.add("法轮", "政治")
        ac.build()
        hits = ac.scan("讨论法轮功的文章")
        assert len(hits) == 1
        assert hits[0].word == "法轮"
        assert hits[0].category == "政治"
        assert hits[0].start == 2
        assert hits[0].end == 4

    def test_multiple_patterns_overlap(self) -> None:
        ac = _AhoCorasick()
        ac.add("观音", "宗教")
        ac.add("观音法门", "暴恐")
        ac.build()
        hits = ac.scan("听说观音法门很神秘")
        words = sorted(h.word for h in hits)
        # 后缀链合并：两条都应命中
        assert words == ["观音", "观音法门"]

    def test_no_match_returns_empty(self) -> None:
        ac = _AhoCorasick()
        ac.add("敏感", "x")
        ac.build()
        assert ac.scan("今天天气晴朗") == []

    def test_empty_pattern_ignored(self) -> None:
        ac = _AhoCorasick()
        ac.add("", "x")
        ac.add("a", "y")
        ac.build()
        assert ac.scan("abc") == [type(ac.scan("a")[0])(word="a", category="y", start=0, end=1)]

    def test_add_after_build_raises(self) -> None:
        ac = _AhoCorasick()
        ac.add("x", "c")
        ac.build()
        with pytest.raises(RuntimeError):
            ac.add("y", "c")

    def test_scan_before_build_raises(self) -> None:
        ac = _AhoCorasick()
        ac.add("x", "c")
        with pytest.raises(RuntimeError):
            ac.scan("xyz")


# ---------------------------------------------------------------------------
# Lexicon loading
# ---------------------------------------------------------------------------


class TestLexiconLoading:
    def test_requires_explicit_lexicon_source(self) -> None:
        with pytest.raises(ValueError, match="requires an explicit lexicon_dir"):
            SensitiveWordMiddleware()

    def test_explicit_words(self) -> None:
        mw = SensitiveWordMiddleware(
            lexicon_dir=None,
            lexicon_words=["禁词A", "禁词B"],
        )
        assert mw.lexicon_size == 2

    def test_file_loader_categories(self, tmp_path: Path) -> None:
        # 1. 写入两个类别文件
        (tmp_path / "政治.txt").write_text("法轮\n# 注释行\n\n敏感词1\n", "utf-8")
        (tmp_path / "色情.txt").write_text("敏感词2\n", "utf-8")
        mw = SensitiveWordMiddleware(lexicon_dir=tmp_path)
        assert mw.lexicon_size == 3
        r = mw.scan_text("提到法轮和敏感词2")
        cats = {(h.word, h.category) for h in r.hits}
        assert ("法轮", "政治") in cats
        assert ("敏感词2", "色情") in cats

    def test_empty_configured_lexicon_is_noop(self, tmp_path: Path) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=tmp_path)
        assert mw.lexicon_size == 0
        assert mw.scan_text("任何东西").matched is False

    def test_missing_lexicon_dir_raises(self, tmp_path: Path) -> None:
        missing_dir = tmp_path / "missing"
        with pytest.raises(FileNotFoundError, match="lexicon_dir is not a directory"):
            SensitiveWordMiddleware(lexicon_dir=missing_dir)

    def test_case_insensitive_default(self) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["BadWord"])
        assert mw.scan_text("Look at this badword here").matched is True

    def test_case_sensitive_opt_in(self) -> None:
        mw = SensitiveWordMiddleware(
            lexicon_dir=None,
            lexicon_words=["BadWord"],
            case_sensitive=True,
        )
        assert mw.scan_text("Look at this badword here").matched is False
        assert mw.scan_text("Look at this BadWord here").matched is True

    def test_extra_words_appended(self, tmp_path: Path) -> None:
        (tmp_path / "base.txt").write_text("aaa\n", "utf-8")
        mw = SensitiveWordMiddleware(
            lexicon_dir=tmp_path,
            extra_words=["bbb"],
        )
        assert mw.lexicon_size == 2


# ---------------------------------------------------------------------------
# 示例词库 = 极小示例（仅 3 个词）
# ---------------------------------------------------------------------------


class TestExampleLexicon:
    def test_example_lexicon_is_three_word_example(self) -> None:
        example_lexicon_dir = Path(__file__).resolve().parents[2] / "examples" / "sensitive_word" / "sensitive_lexicon"

        assert example_lexicon_dir.is_dir()
        mw = SensitiveWordMiddleware(lexicon_dir=example_lexicon_dir)
        # examples 内只保留 3 个示例敏感词
        assert mw.lexicon_size == 3
        for hit_word, category in [("打人", "民生词库"), ("出售雷管", "涉枪涉爆"), ("腐败", "贪腐词库")]:
            r = mw.scan_text(f"前缀{hit_word}后缀")
            assert r.matched is True, hit_word
            assert any(h.word == hit_word and h.category == category for h in r.hits)
        # 不在示例集里的词不拦
        assert mw.scan_text("今天天气真好").matched is False

    def test_no_allowlist_file_in_example_lexicon_dir(self) -> None:
        example_lexicon_dir = Path(__file__).resolve().parents[2] / "examples" / "sensitive_word" / "sensitive_lexicon"
        # 不再有 allowlist 机制
        assert not (example_lexicon_dir / "allowlist.txt").exists()


# ---------------------------------------------------------------------------
# Message scanning
# ---------------------------------------------------------------------------


class TestScanMessages:
    def test_assistant_not_scanned_by_default(self) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"])
        messages = [
            Message(role=Role.ASSISTANT, content=[TextBlock(text="助手说了禁词")]),
            Message(role=Role.USER, content=[TextBlock(text="干净内容")]),
        ]
        result = mw.scan_messages(messages)
        # ASSISTANT 不在默认扫描角色内（输出侧由 after_model 负责）
        assert result.matched is False

    def test_user_message_hit(self) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"])
        result = mw.scan_messages([_user("这里有禁词")])
        assert result.matched is True
        assert result.words == ["禁词"]

    def test_tool_result_str_content_scanned(self) -> None:
        # RFC-0027 A 方案：Role.TOOL 的 ToolResultBlock(str) 内容也被扫描
        from nexau.core.messages import ToolResultBlock

        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["出售雷管"])
        tool_msg = Message(role=Role.TOOL, content=[ToolResultBlock(tool_use_id="t1", content="搜索结果：有人出售雷管")])
        result = mw.scan_messages([tool_msg])
        assert result.matched is True
        assert "出售雷管" in result.words

    def test_tool_result_list_content_scanned(self) -> None:
        # ToolResultBlock 的 content 为 TextBlock 列表时同样被扫描
        from nexau.core.messages import ToolResultBlock

        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["出售雷管"])
        tool_msg = Message(
            role=Role.TOOL,
            content=[ToolResultBlock(tool_use_id="t1", content=[TextBlock(text="渠道：出售雷管")])],
        )
        result = mw.scan_messages([tool_msg])
        assert result.matched is True

    def test_clean_tool_result_not_flagged(self) -> None:
        from nexau.core.messages import ToolResultBlock

        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["出售雷管"])
        tool_msg = Message(role=Role.TOOL, content=[ToolResultBlock(tool_use_id="t1", content="今天天气晴朗")])
        assert mw.scan_messages([tool_msg]).matched is False


# ---------------------------------------------------------------------------
# before_model — 输入侧拦截
# ---------------------------------------------------------------------------


class TestBeforeModel:
    def test_clean_input_passes_through(self) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"])
        result = mw.before_model(_before_input([_user("天气真好")]))
        assert result.force_stop_reason is None
        assert result.messages is None  # 无改动

    def test_blocks_on_input_hit_terminate(self) -> None:
        # 显式选 terminate 保留 RFC-0027 原行为 (默认已改 mask)
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], input_action="terminate")
        result = mw.before_model(_before_input([_user("这里有禁词")]))
        # 1. 设置强制停止信号
        assert result.force_stop_reason is AgentStopReason.ERROR_OCCURRED
        # 2. 末条消息是拒绝文案（executor 取它作为 final_response）
        assert result.messages is not None
        assert result.messages[-1].role == Role.ASSISTANT
        assert "用户输入" in result.messages[-1].get_text_content()

    def test_block_input_false_skips_scan(self) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], block_input=False)
        result = mw.before_model(_before_input([_user("含禁词")]))
        assert result.force_stop_reason is None
        assert result.messages is None

    def test_raise_on_block_input(self) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], raise_on_block=True)
        with pytest.raises(SensitiveContentBlockedError) as exc:
            mw.before_model(_before_input([_user("禁词出现")]))
        assert exc.value.source == "input"
        assert exc.value.scan_result.words == ["禁词"]


# ---------------------------------------------------------------------------
# after_model — 输出侧拦截
# ---------------------------------------------------------------------------


class TestAfterModel:
    def test_clean_output_passes_through(self) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"])
        msgs = [_user("正常问题"), _assistant("一切正常")]
        result = mw.after_model(_after_output(msgs, original_response="一切正常"))
        assert result.force_stop_reason is None
        assert result.messages is None

    def test_blocks_on_output_hit_and_redacts_terminate(self) -> None:
        # 显式选 terminate 保留 RFC-0027 原行为
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], output_action="terminate")
        # executor 已把违规 assistant 末条追加
        msgs = [_user("正常问题"), _assistant("模型偷偷说了禁词")]
        result = mw.after_model(_after_output(msgs, original_response="模型偷偷说了禁词"))
        assert result.force_stop_reason is AgentStopReason.ERROR_OCCURRED
        assert result.messages is not None
        # 违规末条被拒绝文案替换（脱敏），消息数不增加
        assert len(result.messages) == 2
        last = result.messages[-1].get_text_content()
        assert "模型输出" in last
        # 违规原句已被拒绝文案替换（脱敏）；命中词仅以审计 preview 形式出现
        assert "模型偷偷说了" not in last

    def test_block_output_false_skips_scan(self) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], block_output=False)
        msgs = [_user("正常"), _assistant("模型说了禁词但放过")]
        result = mw.after_model(_after_output(msgs, original_response="模型说了禁词但放过"))
        assert result.force_stop_reason is None
        assert result.messages is None

    def test_raise_on_block_output(self) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], raise_on_block=True)
        msgs = [_user("正常"), _assistant("模型禁词")]
        with pytest.raises(SensitiveContentBlockedError) as exc:
            mw.after_model(_after_output(msgs, original_response="模型禁词"))
        assert exc.value.source == "output"


# ---------------------------------------------------------------------------
# 停止原因 = ERROR_OCCURRED + 命中即时发 RunErrorEvent
# ---------------------------------------------------------------------------


class TestEventEmit:
    def _hook_input(self, run_id: str, text: str) -> BeforeModelHookInput:
        agent_state = MagicMock()
        agent_state.run_id = run_id
        return BeforeModelHookInput(
            agent_state=agent_state,
            max_iterations=10,
            current_iteration=0,
            messages=[_user(text)],
        )

    def test_stop_reason_is_error_occurred(self) -> None:
        # terminate 才会设 ERROR_OCCURRED; 默认 mask 不会
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], input_action="terminate")
        result = mw.before_model(self._hook_input("r1", "含禁词"))
        assert result.force_stop_reason is AgentStopReason.ERROR_OCCURRED

    def test_emits_content_blocked_event_when_emitter_wired(self) -> None:
        from nexau.archs.llm.llm_aggregators.events import ContentBlockedEvent

        captured: list[object] = []
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"])
        mw.set_event_emitter(captured.append)
        mw.before_model(self._hook_input("run_x", "含禁词"))
        assert len(captured) == 1
        event = captured[0]
        assert isinstance(event, ContentBlockedEvent)
        assert event.run_id == "run_x"
        assert event.source == "input"
        assert "禁词" in event.words

    def test_no_emit_without_emitter(self) -> None:
        # 未装载事件中间件（无 emitter）时静默跳过，不报错
        # 显式选 terminate 以验证 ERROR_OCCURRED 仍生效
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], input_action="terminate")
        result = mw.before_model(self._hook_input("r1", "含禁词"))
        assert result.force_stop_reason is AgentStopReason.ERROR_OCCURRED  # 仍然拦截

    def test_clean_input_emits_nothing(self) -> None:
        captured: list[object] = []
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"])
        mw.set_event_emitter(captured.append)
        mw.before_model(self._hook_input("r1", "天气真好"))
        assert captured == []


# ---------------------------------------------------------------------------
# OnHitAction: mask — 替换敏感词后 run 继续 (默认动作)
# ---------------------------------------------------------------------------


class TestMaskAction:
    def test_default_action_is_mask(self) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"])
        result = mw.before_model(_before_input([_user("这里有禁词请别拦")]))
        # mask 不设 force_stop_reason, run 继续
        assert result.force_stop_reason is None
        assert result.messages is not None
        masked = result.messages[-1].get_text_content()
        # 敏感词被替换
        assert "禁词" not in masked
        # 上下文保留
        assert "这里有" in masked
        assert "请别拦" in masked

    def test_mask_input_uses_star_placeholder_by_default(self) -> None:
        # 默认 mask_template = "***" (密码遮挡通用约定)
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], input_action="mask")
        result = mw.before_model(_before_input([_user("这里有禁词")]))
        assert result.messages is not None
        masked = result.messages[0].get_text_content()
        assert "***" in masked
        assert "禁词" not in masked

    def test_mask_input_opt_in_category_label_template(self) -> None:
        # 显式传 "[<{category}>]" opt-in 类别审计标签 (取代旧默认值)
        mw = SensitiveWordMiddleware(
            lexicon_dir=None,
            lexicon_words=["禁词"],
            input_action="mask",
            mask_template="[<{category}>]",
        )
        result = mw.before_model(_before_input([_user("这里有禁词")]))
        assert result.messages is not None
        assert "[<explicit>]" in result.messages[0].get_text_content()

    def test_mask_input_custom_template(self) -> None:
        mw = SensitiveWordMiddleware(
            lexicon_dir=None,
            lexicon_words=["禁词"],
            input_action="mask",
            mask_template="***",
        )
        result = mw.before_model(_before_input([_user("这里有禁词")]))
        assert result.messages is not None
        assert "***" in result.messages[0].get_text_content()
        assert "禁词" not in result.messages[0].get_text_content()

    def test_mask_output_replaces_in_assistant_message(self) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], output_action="mask")
        msgs = [_user("正常"), _assistant("模型偷偷说了禁词后又说了正常话")]
        result = mw.after_model(_after_output(msgs, original_response="模型偷偷说了禁词后又说了正常话"))
        assert result.force_stop_reason is None  # mask 不终止
        assert result.messages is not None
        last = result.messages[-1].get_text_content()
        assert "禁词" not in last
        # mask 保留了 assistant 的正常内容,而不是整段被拒绝文案替换
        assert "模型偷偷说了" in last
        assert "后又说了正常话" in last

    def test_mask_handles_overlapping_hits(self) -> None:
        # 覆盖 "观音" 和 "观音法门" 重叠场景
        mw = SensitiveWordMiddleware(
            lexicon_dir=None,
            lexicon_words=["观音", "观音法门"],
            input_action="mask",
            mask_template="***",
        )
        result = mw.before_model(_before_input([_user("听说观音法门很神秘")]))
        assert result.messages is not None
        masked = result.messages[0].get_text_content()
        # 重叠的两个 hit 合并成一个 mask 区, 不重复 mask
        assert masked.count("***") == 1
        assert "观音" not in masked
        assert "听说" in masked
        assert "很神秘" in masked

    def test_mask_overlapping_hits_word_placeholder_uses_merged_span(self) -> None:
        # 重叠 hit 合并后, {word}/{length} 模板应渲染合并后的完整子串,
        # 而不是前一个较短的词 ("观音")
        mw = SensitiveWordMiddleware(
            lexicon_dir=None,
            lexicon_words=["观音", "观音法门"],
            input_action="mask",
            mask_template="[{word}:{length}]",
        )
        result = mw.before_model(_before_input([_user("听说观音法门很神秘")]))
        assert result.messages is not None
        masked = result.messages[0].get_text_content()
        assert "[观音法门:4]" in masked
        assert "[观音:2]" not in masked

    def test_mask_preserves_other_messages(self) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], input_action="mask")
        result = mw.before_model(_before_input([_user("第一条干净"), _user("第二条有禁词"), _user("第三条也干净")]))
        assert result.messages is not None
        assert "第一条干净" in result.messages[0].get_text_content()
        # 第二条的敏感词被替换
        assert "禁词" not in result.messages[1].get_text_content()
        assert "第二条有" in result.messages[1].get_text_content()
        assert "第三条也干净" in result.messages[2].get_text_content()

    def test_mask_tool_result_str_content(self) -> None:
        # ToolResultBlock.content 是 str 的 case
        from nexau.core.messages import ToolResultBlock

        mw = SensitiveWordMiddleware(
            lexicon_dir=None,
            lexicon_words=["出售雷管"],
            input_action="mask",
            mask_template="***",
        )
        tool_msg = Message(
            role=Role.TOOL,
            content=[
                ToolResultBlock(
                    tool_use_id="t1",
                    content="搜索结果：有人出售雷管",
                    raw_output={"rows": [1, 2, 3]},
                )
            ],
        )
        result = mw.before_model(_before_input([tool_msg]))
        assert result.force_stop_reason is None
        assert result.messages is not None
        new_msg = result.messages[0]
        new_tool_block = new_msg.content[0]
        assert isinstance(new_tool_block, ToolResultBlock)
        assert new_tool_block.content == "搜索结果：有人***"
        assert new_tool_block.tool_use_id == "t1"
        # 脱敏后仍保留结构化 raw_output, 以及原消息的 id/created_at
        assert new_tool_block.raw_output == {"rows": [1, 2, 3]}
        assert new_msg.id == tool_msg.id
        assert new_msg.created_at == tool_msg.created_at

    def test_mask_tool_result_list_content(self) -> None:
        # ToolResultBlock.content 是 list[TextBlock] 的 case
        from nexau.core.messages import ToolResultBlock

        mw = SensitiveWordMiddleware(
            lexicon_dir=None,
            lexicon_words=["出售雷管"],
            input_action="mask",
            mask_template="***",
        )
        tool_msg = Message(
            role=Role.TOOL,
            content=[
                ToolResultBlock(
                    tool_use_id="t1",
                    content=[TextBlock(text="出售雷管渠道"), TextBlock(text="干净文本")],
                    raw_output={"rows": [1, 2, 3]},
                )
            ],
        )
        result = mw.before_model(_before_input([tool_msg]))
        assert result.force_stop_reason is None
        assert result.messages is not None
        new_tool_block = result.messages[0].content[0]
        assert isinstance(new_tool_block, ToolResultBlock)
        assert isinstance(new_tool_block.content, list)
        # list content 分支同样保留 raw_output
        assert new_tool_block.raw_output == {"rows": [1, 2, 3]}
        # 第 1 个 inner block 被 mask, 第 2 个干净 block 保持原样
        first = new_tool_block.content[0]
        assert isinstance(first, TextBlock)
        assert first.text == "***渠道"
        second = new_tool_block.content[1]
        assert isinstance(second, TextBlock)
        assert second.text == "干净文本"

    def test_mask_tool_result_clean_content_no_change(self) -> None:
        # ToolResultBlock 内容无命中时该 message 原样保留 (any_changed=False 路径)
        from nexau.core.messages import ToolResultBlock

        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], input_action="mask")
        clean_tool = ToolResultBlock(tool_use_id="t1", content="完全干净")
        tool_msg = Message(role=Role.TOOL, content=[clean_tool])
        result = mw.before_model(_before_input([_user("含禁词"), tool_msg]))
        assert result.messages is not None
        # tool_msg 没命中, 整个 Message 原样返回 (走 any_changed=False 路径)
        assert result.messages[1].content[0] is clean_tool


# ---------------------------------------------------------------------------
# OnHitAction: soft_reject — 温和拒绝, terminal_reason=SUCCESS 不是错误
# ---------------------------------------------------------------------------


class TestSoftRejectAction:
    def test_soft_reject_input_uses_success_not_error(self) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], input_action="soft_reject")
        result = mw.before_model(_before_input([_user("含禁词")]))
        # 关键: SUCCESS 不是 ERROR_OCCURRED
        assert result.force_stop_reason is AgentStopReason.SUCCESS
        assert result.messages is not None
        last = result.messages[-1].get_text_content()
        # 温和文案,不是错误式的
        assert "不便回答" in last or "跳过" in last

    def test_soft_reject_output_replaces_assistant(self) -> None:
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], output_action="soft_reject")
        msgs = [_user("正常"), _assistant("模型说了禁词")]
        result = mw.after_model(_after_output(msgs, original_response="模型说了禁词"))
        assert result.force_stop_reason is AgentStopReason.SUCCESS
        assert result.messages is not None
        last = result.messages[-1].get_text_content()
        assert "禁词" not in last
        # 整条 assistant 被替换 (不是 mask 那种保留正常部分)
        assert "模型说了" not in last

    def test_soft_reject_masks_history_to_avoid_repeat_trigger(self) -> None:
        """soft_reject 入口必须 mask 历史里命中的敏感词, 否则下一轮 history 残留
        会再次触发 soft_reject 导致"永远聊不下去"。
        """
        mw = SensitiveWordMiddleware(lexicon_dir=None, lexicon_words=["禁词"], input_action="soft_reject")
        # 模拟第一轮 user 命中
        result = mw.before_model(_before_input([_user("我想问禁词")]))
        assert result.messages is not None
        assert result.force_stop_reason is AgentStopReason.SUCCESS
        # 关键: 返回的 messages 里 user 已经被 mask, 不再含原敏感词
        # (这些 messages 会被持久化, 下轮再扫不会触发同一敏感词)
        assert all("禁词" not in m.get_text_content() for m in result.messages if m.role == Role.USER)
        # 末条仍是温和拒绝
        assert result.messages[-1].role == Role.ASSISTANT
        assert "不便回答" in result.messages[-1].get_text_content() or "跳过" in result.messages[-1].get_text_content()

    def test_soft_reject_custom_template(self) -> None:
        mw = SensitiveWordMiddleware(
            lexicon_dir=None,
            lexicon_words=["禁词"],
            input_action="soft_reject",
            soft_refusal_template="本话题不能聊喔, 换个问法试试 (命中 {hits})",
        )
        result = mw.before_model(_before_input([_user("说点禁词")]))
        assert result.messages is not None
        last = result.messages[-1].get_text_content()
        assert "本话题不能聊喔" in last
        assert "禁词" in last  # hits preview


# ---------------------------------------------------------------------------
# Action 不同组合: input/output 各自独立
# ---------------------------------------------------------------------------


class TestActionMixing:
    def test_input_mask_output_terminate(self) -> None:
        # input 路径 mask, output 路径 terminate — 各自独立生效
        mw = SensitiveWordMiddleware(
            lexicon_dir=None,
            lexicon_words=["禁词"],
            input_action="mask",
            output_action="terminate",
        )
        # input: mask, 不终止
        r_in = mw.before_model(_before_input([_user("含禁词")]))
        assert r_in.force_stop_reason is None
        # output: terminate, ERROR_OCCURRED
        r_out = mw.after_model(_after_output([_user("正常"), _assistant("禁词")], original_response="禁词"))
        assert r_out.force_stop_reason is AgentStopReason.ERROR_OCCURRED


# ---------------------------------------------------------------------------
# 端到端：通过 Agent.run 走完整 async executor，验证 RFC-0027 强制停止短路
# ---------------------------------------------------------------------------


class TestExecutorIntegration:
    """走真实 execute_async 路径，验证 force_stop_reason 短路生效。"""

    def _agent(self):
        from unittest.mock import Mock, patch

        from nexau import Agent, AgentConfig
        from nexau.archs.llm.llm_config import LLMConfig

        with patch("nexau.archs.main_sub.agent.openai") as mock_openai:
            mock_openai.OpenAI.return_value = Mock()
            config = AgentConfig(
                name="sw_agent",
                llm_config=LLMConfig(model="gpt-4o-mini"),
                tool_call_mode="openai",
                middlewares=[
                    SensitiveWordMiddleware(
                        lexicon_dir=None,
                        lexicon_words=["禁词"],
                        # 这些 e2e 测试验证 RFC-0027 终止短路, 显式选 terminate
                        input_action="terminate",
                        output_action="terminate",
                    )
                ],
            )
            return Agent(config=config)

    def test_input_hit_short_circuits_before_model_call(self) -> None:
        from unittest.mock import AsyncMock, patch

        agent = self._agent()
        with patch.object(agent.executor.llm_caller, "call_llm_async", new_callable=AsyncMock) as mock_call:
            response = agent.run(message="这里有禁词")

        # before_model 命中 → 模型根本不应被调用
        mock_call.assert_not_called()
        assert "用户输入" in response

    def test_output_hit_blocks_after_model_call(self) -> None:
        from unittest.mock import AsyncMock, patch

        from nexau.archs.main_sub.execution.model_response import ModelResponse

        agent = self._agent()
        with patch.object(
            agent.executor.llm_caller,
            "call_llm_async",
            new_callable=AsyncMock,
            side_effect=[ModelResponse(content="模型偷偷说了禁词")],
        ) as mock_call:
            response = agent.run(message="正常问题")

        mock_call.assert_called_once()
        assert "模型输出" in response
        assert "模型偷偷说了" not in response  # 违规原句已脱敏
