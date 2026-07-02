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

"""Sensitive-word middleware.

RFC-0027: 敏感词中间件

在模型调用前后扫描文本：``before_model`` 拦用户输入，``after_model`` 拦模型输出。
命中时根据 ``input_action`` / ``output_action`` 选择三种动作之一，命中事件
始终通过 ``ContentBlockedEvent`` 上报（与终止用的 ``RunErrorEvent`` 区分）。

命中动作 (``OnHitAction``)：

- ``terminate``：设 ``force_stop_reason=ERROR_OCCURRED``, 用 ``refusal_template``
  拒绝文案替换/追加 assistant 消息, executor 把本次 run 当作错误终止。RFC-0027
  原始行为, 适合严苛合规场景 (审计/政府客户)。
- ``mask`` (默认)：把命中位置在原文中替换为 ``mask_template`` (默认 ``***``),
  assistant 看到的是脱敏后的输入, run 正常继续。是大部分对话场景的合理 fallback,
  让用户的对话不会被一个敏感词整轮打断。需要审计标签时显式传
  ``mask_template="[<{category}>]"``。
- ``soft_reject``：用 ``soft_refusal_template`` 拒绝文案替换/追加 assistant
  消息, force_stop_reason=``SUCCESS``。run 算正常完成 (不是错误), 调用方拿到的
  ``terminal_reason`` 与正常对话一致, 只是末条回复是温和拒绝。**input 路径**额外
  把 history 里命中的敏感词 mask 掉, 这样下一轮 user 换话题时 history 残留
  不会再触发同一拒绝 (语义: 礼貌跳过当轮, 后续可继续)。适合"不想报错也不想
  让 LLM 看到原文"的场景。

词库来源必须显式配置：支持自定义目录、单文件或显式词表。

不依赖任何第三方包，内置一个轻量级 Aho-Corasick 自动机用于多模式匹配。
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from nexau.archs.llm.llm_aggregators.events import ContentBlockedEvent
from nexau.archs.main_sub.execution.hooks import (
    AfterModelHookInput,
    BeforeModelHookInput,
    HookResult,
    Middleware,
)
from nexau.archs.main_sub.execution.stop_reason import AgentStopReason
from nexau.core.messages import Message, Role, TextBlock, ToolResultBlock

if TYPE_CHECKING:
    from nexau.archs.main_sub.agent_state import AgentState

logger = logging.getLogger(__name__)


class OnHitAction(str, Enum):
    """RFC-0027 follow-up: 命中敏感词时的处理动作。

    见模块 docstring 的详细说明。
    """

    TERMINATE = "terminate"
    MASK = "mask"
    SOFT_REJECT = "soft_reject"


# 默认 terminate / soft_reject 文案（参考 konsheng/Sensitive-lexicon 的语义分类作为提示模板）
_DEFAULT_REFUSAL_TEMPLATE = (
    "⚠️ 内容安全提示：检测到{source}包含「{category}」类敏感词（命中 {hits}），"
    "已按照内部内容安全策略中断本次请求。\n\n"
    "如确有合规业务需要，请联系内容安全负责人调整词库或申请白名单。"
)

# 默认 soft_reject 文案 (比 terminate 文案更柔和, 不暗示"错误"语义)
_DEFAULT_SOFT_REFUSAL_TEMPLATE = "抱歉, {source}涉及不便回答的话题, 跳过本轮。如有合规业务需要欢迎换个问法或联系内容安全负责人。"

# 默认 mask 占位符: 密码遮挡的通用约定, 简短中性, LLM 看到也容易理解。
# 想带类别审计标签的, 显式传 ``mask_template="[<{category}>]"``;
# 想按原词长度自适应的, 传 ``mask_template="*{length}*"`` 之类。
_DEFAULT_MASK_TEMPLATE = "***"

# 默认扫描的角色：用户输入侧 + 工具结果（RFC-0027 A 方案：拦截 tool result）。
# TOOL 消息的文本在 ToolResultBlock 里，scan_messages 会专门抽取（见 _extract_scan_text）。
_DEFAULT_SCAN_ROLES: frozenset[Role] = frozenset({Role.USER, Role.FRAMEWORK, Role.SYSTEM, Role.TOOL})


# ---------------------------------------------------------------------------
# 公共数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SensitiveHit:
    """A single sensitive-word match.

    RFC-0027: 单条敏感词命中记录
    """

    word: str
    category: str
    start: int
    end: int


def _empty_hit_list() -> list[SensitiveHit]:
    return []


@dataclass
class SensitiveScanResult:
    """All hits for a scanned text payload.

    RFC-0027: 单次扫描结果聚合
    """

    hits: list[SensitiveHit] = field(default_factory=_empty_hit_list)

    @property
    def matched(self) -> bool:
        return bool(self.hits)

    @property
    def categories(self) -> list[str]:
        seen: dict[str, None] = {}
        for hit in self.hits:
            seen.setdefault(hit.category, None)
        return list(seen.keys())

    @property
    def words(self) -> list[str]:
        seen: dict[str, None] = {}
        for hit in self.hits:
            seen.setdefault(hit.word, None)
        return list(seen.keys())


class SensitiveContentBlockedError(RuntimeError):
    """Raised internally when a sensitive hit is found.

    RFC-0027: 中间件内部用异常承载命中事件
    """

    def __init__(self, source: str, scan_result: SensitiveScanResult) -> None:
        super().__init__(f"sensitive content blocked: source={source} hits={scan_result.words}")
        self.source = source
        self.scan_result = scan_result


# ---------------------------------------------------------------------------
# Aho-Corasick 多模式匹配器（纯 Python，无外部依赖）
# ---------------------------------------------------------------------------


class _AhoCorasick:
    """Minimal Aho-Corasick automaton for multi-pattern Chinese/English matching.

    RFC-0027: 多模式匹配自动机

    - 节点用 dict 存储 children，避免数组开销并自然支持 Unicode
    - 每个 pattern 关联一个 ``category`` 标签，命中时返回
    - 构建复杂度 O(Σ|patterns|)，单次扫描 O(|text| + matches)
    """

    __slots__ = ("_children", "_fail", "_output", "_built")

    def __init__(self) -> None:
        # 节点 0 为根
        self._children: list[dict[str, int]] = [{}]
        # fail 链：每个节点指向最长真后缀对应的节点
        self._fail: list[int] = [0]
        # 节点结束时输出的 (word, category) 列表
        self._output: list[list[tuple[str, str]]] = [[]]
        self._built = False

    def add(self, word: str, category: str) -> None:
        # 1. 不允许在构建后追加，避免 fail 链失效
        if self._built:
            raise RuntimeError("cannot add patterns after build()")
        if not word:
            return

        # 2. 沿 trie 走/建节点
        node = 0
        for ch in word:
            nxt = self._children[node].get(ch)
            if nxt is None:
                self._children.append({})
                self._fail.append(0)
                self._output.append([])
                nxt = len(self._children) - 1
                self._children[node][ch] = nxt
            node = nxt

        # 3. 终止节点登记 (word, category)
        self._output[node].append((word, category))

    def build(self) -> None:
        """Compute fail links via BFS. Must be called before scan()."""
        if self._built:
            return

        # 1. 根的所有直接子节点 fail 指回根
        queue: deque[int] = deque()
        for child in self._children[0].values():
            self._fail[child] = 0
            queue.append(child)

        # 2. BFS：对每个节点 u，遍历其 children
        while queue:
            u = queue.popleft()
            for ch, v in self._children[u].items():
                # 沿 fail 链找最长真后缀
                f = self._fail[u]
                while f != 0 and ch not in self._children[f]:
                    f = self._fail[f]
                candidate = self._children[f].get(ch, 0)
                # 避免自指（v 自己不能作为自己的 fail）
                self._fail[v] = candidate if candidate != v else 0
                # 输出合并（后缀链上的命中也要触发）
                self._output[v].extend(self._output[self._fail[v]])
                queue.append(v)

        self._built = True

    def scan(self, text: str) -> list[SensitiveHit]:
        """Scan text and return all hits."""
        if not self._built:
            raise RuntimeError("AhoCorasick.scan() called before build()")
        if not text:
            return []

        hits: list[SensitiveHit] = []
        node = 0
        for i, ch in enumerate(text):
            # 1. 沿 fail 链找下一个匹配节点
            while node and ch not in self._children[node]:
                node = self._fail[node]
            node = self._children[node].get(ch, 0)

            # 2. 收集本节点的所有输出
            if self._output[node]:
                for word, category in self._output[node]:
                    end = i + 1
                    hits.append(SensitiveHit(word=word, category=category, start=end - len(word), end=end))
        return hits


# ---------------------------------------------------------------------------
# 大小写归一（保长）
# ---------------------------------------------------------------------------


def _casefold_lower(text: str) -> str:
    """Length-preserving lowercase for case-insensitive matching.

    RFC-0027 安全修复：``str.lower()`` 对个别 Unicode 字符会**改变码点数**——
    唯一常见反例是 ``"İ"``（U+0130）``.lower()`` → ``"i̇"``（``i`` + U+0307 组合点，
    2 个码点）。敏感词 mask 用 lower 串上的 AC 命中 offset 去切**原文**，一旦出现这类
    变长字符，offset 相对原文右移，mask 窗口被推离目标词——垫 L 个 ``İ``（L=词长）即可
    把违禁词整块推出 mask 区、原样进 LLM 与 history。

    这里逐字符归一并强制 **1:1**：若 ``ch.lower()`` 不是单码点则保留原字符，保证归一串
    与原文逐位对齐，AC offset 可直接用于原文切片。词库归一必须走同一函数，否则命中条件不一致。
    """
    out: list[str] = []
    for ch in text:
        low = ch.lower()
        out.append(low if len(low) == 1 else ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# 词库加载
# ---------------------------------------------------------------------------


def _load_words_from_file(path: Path) -> Iterable[str]:
    """Yield non-empty, non-comment lines from a UTF-8 lexicon file."""
    raw = path.read_text("utf-8", errors="replace")
    for line in raw.splitlines():
        word = line.strip()
        if not word or word.startswith("#"):
            continue
        yield word


def _load_lexicon(
    *,
    lexicon_dir: Path | None,
    lexicon_file: Path | None,
    lexicon_words: Iterable[str] | None,
    extra_words: Iterable[str] | None,
    case_sensitive: bool,
) -> dict[str, str]:
    """Aggregate all sources into a {word: category} mapping."""
    out: dict[str, str] = {}

    def _normalize(word: str) -> str:
        return word if case_sensitive else _casefold_lower(word)

    def _put(word: str, category: str) -> None:
        norm = _normalize(word.strip())
        if norm:
            # 同词后注入的类别覆盖前者
            out[norm] = category

    # 1. 显式词表（最高优先级，统一类别 "explicit"）
    if lexicon_words is not None:
        for w in lexicon_words:
            _put(w, "explicit")

    # 2. 单文件
    if lexicon_file is not None:
        category = lexicon_file.stem
        for w in _load_words_from_file(lexicon_file):
            _put(w, category)

    # 3. 目录（遍历所有 *.txt，文件名为 category）
    if lexicon_dir is not None:
        if not lexicon_dir.is_dir():
            raise FileNotFoundError(f"SensitiveWordMiddleware lexicon_dir is not a directory: {lexicon_dir}")
        for child in sorted(lexicon_dir.glob("*.txt")):
            category = child.stem
            for w in _load_words_from_file(child):
                _put(w, category)

    # 4. 额外补充词表（追加敏感词，归入通用 "extra" 类）
    if extra_words is not None:
        for w in extra_words:
            _put(w, "extra")

    return out


# ---------------------------------------------------------------------------
# 中间件主体
# ---------------------------------------------------------------------------


class SensitiveWordMiddleware(Middleware):
    """Block LLM input/output containing words from a sensitive lexicon.

    RFC-0027: 敏感词中间件

    用法（最常见配置：显式传入词库目录，输入输出全拦截）::

        from nexau.archs.main_sub.execution.middleware.sensitive_word import (
            SensitiveWordMiddleware,
        )

        agent_config.middlewares.append(SensitiveWordMiddleware(lexicon_dir="/opt/nexau/sensitive_lexicon"))

    Args:
        lexicon_dir: 词库目录，目录下每个 ``.txt`` 文件以文件名作为类别，
            一行一个词。必须与 ``lexicon_file`` / ``lexicon_words`` 三者至少配置一个。
        lexicon_file: 单文件词库路径，文件名作为类别。
        lexicon_words: 直接传入的词表，统一归入 "explicit" 类。
        extra_words: 追加的补充敏感词，归入 "extra" 类。
        case_sensitive: 是否区分大小写。中文不受影响；英文敏感词建议 ``False``。
        block_input: 是否扫描 LLM 入参（``scan_roles`` 指定的角色，默认含用户/系统/框架消息 + 工具结果）。
        block_output: 是否扫描 LLM 回复正文。
        input_action: 输入命中时的动作 (``OnHitAction``)，默认 ``mask`` (脱敏后让对话继续)。
            老用户要保留 RFC-0027 原行为请显式传 ``"terminate"``。
        output_action: 输出命中时的动作，默认 ``mask`` (脱敏后让对话继续)。
        refusal_template: ``terminate`` 动作的拒绝文案模板，可用 ``{source}`` /
            ``{category}`` / ``{hits}`` 三个占位符。
        soft_refusal_template: ``soft_reject`` 动作的温和拒绝文案模板，占位符同
            ``refusal_template``。
        mask_template: ``mask`` 动作的占位符模板，可用 ``{category}`` /
            ``{word}`` / ``{length}`` (原词长度) 占位符。默认 ``"***"`` (密码遮挡
            通用约定, 简短中性)。需要带审计类别标签的, 显式传 ``"[<{category}>]"``;
            想按原词长度自适应的, 传 ``"*{length}*"`` 之类。
        scan_roles: 入参扫描覆盖的 ``Role`` 集合；默认 USER/FRAMEWORK/SYSTEM/TOOL（含工具结果）。
        raise_on_block: True 时命中直接抛 ``SensitiveContentBlockedError``；
            默认 False，走"按 action 处理"的优雅路径。
    """

    source_id = "sensitive_word_middleware"

    def __init__(
        self,
        *,
        lexicon_dir: Path | str | None = None,
        lexicon_file: Path | str | None = None,
        lexicon_words: Iterable[str] | None = None,
        extra_words: Iterable[str] | None = None,
        case_sensitive: bool = False,
        block_input: bool = True,
        block_output: bool = True,
        input_action: OnHitAction | str = OnHitAction.MASK,
        output_action: OnHitAction | str = OnHitAction.MASK,
        refusal_template: str = _DEFAULT_REFUSAL_TEMPLATE,
        soft_refusal_template: str = _DEFAULT_SOFT_REFUSAL_TEMPLATE,
        mask_template: str = _DEFAULT_MASK_TEMPLATE,
        scan_roles: Iterable[Role] | None = None,
        raise_on_block: bool = False,
    ) -> None:
        if lexicon_dir is None and lexicon_file is None and lexicon_words is None:
            raise ValueError(
                "SensitiveWordMiddleware requires an explicit lexicon_dir, lexicon_file, "
                "or lexicon_words; no default sensitive lexicon is bundled."
            )

        # 1. 路径参数归一化
        norm_dir = Path(lexicon_dir) if lexicon_dir is not None else None
        norm_file = Path(lexicon_file) if lexicon_file is not None else None

        # 2. 加载词库
        words = _load_lexicon(
            lexicon_dir=norm_dir,
            lexicon_file=norm_file,
            lexicon_words=lexicon_words,
            extra_words=extra_words,
            case_sensitive=case_sensitive,
        )

        # 3. 构建 AC 自动机
        automaton = _AhoCorasick()
        for word, category in words.items():
            automaton.add(word, category)
        automaton.build()

        # 4. 落字段
        self._automaton = automaton
        self._lexicon_size = len(words)
        self._case_sensitive = case_sensitive
        self._block_input = block_input
        self._block_output = block_output
        self._input_action = OnHitAction(input_action) if not isinstance(input_action, OnHitAction) else input_action
        self._output_action = OnHitAction(output_action) if not isinstance(output_action, OnHitAction) else output_action
        self._refusal_template = refusal_template
        self._soft_refusal_template = soft_refusal_template
        self._mask_template = mask_template
        self._scan_roles: frozenset[Role] = frozenset(scan_roles) if scan_roles is not None else _DEFAULT_SCAN_ROLES
        self._raise_on_block = raise_on_block
        # RFC-0027: 由 executor 注入的统一事件发射器（命中时发 ContentBlockedEvent）。
        self._event_emitter: Callable[[object], None] | None = None

        logger.info(
            "[SensitiveWordMiddleware] loaded %d words; block_input=%s/%s block_output=%s/%s case_sensitive=%s",
            self._lexicon_size,
            block_input,
            self._input_action.value,
            block_output,
            self._output_action.value,
            case_sensitive,
        )

    # ------------------------------------------------------------------
    # 对外查询接口（便于测试）
    # ------------------------------------------------------------------

    @property
    def lexicon_size(self) -> int:
        return self._lexicon_size

    def scan_text(self, text: str) -> SensitiveScanResult:
        """Scan a raw string and return all hits.

        RFC-0027: 单文本扫描入口（测试 / 离线审计可用）
        """
        if not text or self._lexicon_size == 0:
            return SensitiveScanResult()
        haystack = text if self._case_sensitive else _casefold_lower(text)
        return SensitiveScanResult(hits=self._automaton.scan(haystack))

    @staticmethod
    def _extract_scan_text(msg: Message) -> str:
        """Extract scannable text from a message, including tool-result blocks.

        RFC-0027: ``Message.get_text_content()`` 只取 TextBlock；工具结果文本在
        ``ToolResultBlock.content`` 里（str 或 TextBlock/ImageBlock 列表），需单独抽取，
        否则 ``Role.TOOL`` 消息扫不到内容。
        """
        parts: list[str] = [msg.get_text_content()]
        for block in msg.content:
            if isinstance(block, ToolResultBlock):
                content = block.content
                if isinstance(content, str):
                    parts.append(content)
                else:
                    parts.extend(p.text for p in content if isinstance(p, TextBlock))
        return "".join(parts)

    def scan_messages(self, messages: list[Message]) -> SensitiveScanResult:
        """Scan all messages whose role is in ``scan_roles``.

        RFC-0027: 消息列表扫描入口
        """
        if self._lexicon_size == 0:
            return SensitiveScanResult()

        aggregated_hits: list[SensitiveHit] = []
        offset = 0
        for msg in messages:
            if msg.role not in self._scan_roles:
                continue
            text = self._extract_scan_text(msg)
            if not text:
                continue
            for hit in self.scan_text(text).hits:
                aggregated_hits.append(
                    SensitiveHit(
                        word=hit.word,
                        category=hit.category,
                        start=offset + hit.start,
                        end=offset + hit.end,
                    )
                )
            offset += len(text) + 1  # +1 模拟分隔符

        return SensitiveScanResult(hits=aggregated_hits)

    # ------------------------------------------------------------------
    # Mask 文本工具
    # ------------------------------------------------------------------

    def _format_placeholder(self, hit: SensitiveHit) -> str:
        """Render mask_template for a single hit; ignore unknown placeholders."""
        try:
            return self._mask_template.format(
                category=hit.category,
                word=hit.word,
                length=len(hit.word),
            )
        except (KeyError, IndexError):
            # mask_template 不含占位符 (如 "***") → format 返回原样
            return self._mask_template

    def _mask_text(self, text: str) -> tuple[str, SensitiveScanResult]:
        """Scan ``text`` and return (masked_text, scan_result).

        AC 命中的 ``(start, end)`` 反向遍历替换, 避开 offset 漂移。case_sensitive=False
        时 scan 用的是归一后的 haystack; 归一走 :func:`_casefold_lower`（**保长** 1:1），
        故命中 offset 与原文逐位对齐, 原 text 直接 ``[start:end]`` 替换是对的。
        （注: 不能用 ``str.lower()``——``"İ"`` 等字符会展开码点使 offset 漂移, 违禁词可绕过 mask。）
        """
        result = self.scan_text(text)
        if not result.matched:
            return text, result
        # 多个 hit 可能重叠 (e.g. "观音" 和 "观音法门") — 取覆盖范围, 避免重复 mask
        # 按 start 升序 / end 降序排, 跳过被前面包含的 hit
        sorted_hits = sorted(result.hits, key=lambda h: (h.start, -h.end))
        merged: list[SensitiveHit] = []
        for hit in sorted_hits:
            if merged and hit.start < merged[-1].end:
                # 与前一个重叠; 保留更长的那个 (前一个范围更大)
                if hit.end > merged[-1].end:
                    # 扩展覆盖范围; word 取合并后的实际子串, 否则自定义
                    # mask_template 里的 {word}/{length} 会渲染成旧的短词
                    merged[-1] = SensitiveHit(
                        word=text[merged[-1].start : hit.end],
                        category=merged[-1].category,
                        start=merged[-1].start,
                        end=hit.end,
                    )
                continue
            merged.append(hit)
        # 反向替换
        out = text
        for hit in reversed(merged):
            out = out[: hit.start] + self._format_placeholder(hit) + out[hit.end :]
        return out, result

    def _mask_message(self, msg: Message) -> Message | None:
        """Return a new Message with sensitive content masked; None if no change."""
        from nexau.core.messages import DiscriminatedBlock, ToolResultContentBlock

        any_changed = False
        new_blocks: list[DiscriminatedBlock] = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                masked, result = self._mask_text(block.text)
                if result.matched:
                    any_changed = True
                    new_blocks.append(TextBlock(id=block.id, text=masked))
                else:
                    new_blocks.append(block)
            elif isinstance(block, ToolResultBlock):
                content = block.content
                if isinstance(content, str):
                    masked, result = self._mask_text(content)
                    if result.matched:
                        any_changed = True
                        new_blocks.append(
                            ToolResultBlock(
                                tool_use_id=block.tool_use_id,
                                content=masked,
                                is_error=block.is_error,
                                raw_output=block.raw_output,
                            )
                        )
                    else:
                        new_blocks.append(block)
                else:
                    sub_blocks: list[ToolResultContentBlock] = []
                    sub_changed = False
                    for inner in content:
                        if isinstance(inner, TextBlock):
                            masked, result = self._mask_text(inner.text)
                            if result.matched:
                                sub_changed = True
                                sub_blocks.append(TextBlock(id=inner.id, text=masked))
                            else:
                                sub_blocks.append(inner)
                        else:
                            sub_blocks.append(inner)
                    if sub_changed:
                        any_changed = True
                        new_blocks.append(
                            ToolResultBlock(
                                tool_use_id=block.tool_use_id,
                                content=sub_blocks,
                                is_error=block.is_error,
                                raw_output=block.raw_output,
                            )
                        )
                    else:
                        new_blocks.append(block)
            else:
                new_blocks.append(block)
        if not any_changed:
            return None
        return Message(
            id=msg.id,
            role=msg.role,
            content=new_blocks,
            metadata=msg.metadata,
            created_at=msg.created_at,
        )

    # ------------------------------------------------------------------
    # Hook：before_model 拦输入，after_model 拦输出
    # ------------------------------------------------------------------

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:  # type: ignore[override]
        """Scan input messages before the LLM call; act per ``input_action`` on hit.

        RFC-0027: 输入侧拦截

        terminate: 设 force_stop_reason=ERROR_OCCURRED + 追加拒绝文案。
        mask: 把命中的敏感词在 messages 内替换成占位符, run 继续。
        soft_reject: 用温和拒绝文案 + force_stop_reason=SUCCESS (正常完成而不是错误),
            同时 mask history 命中位置避免后续轮再次触发。
        """
        if not self._block_input or self._lexicon_size == 0:
            return HookResult.no_changes()

        result = self.scan_messages(list(hook_input.messages))
        if not result.matched:
            return HookResult.no_changes()

        if self._raise_on_block:
            raise SensitiveContentBlockedError(source="input", scan_result=result)

        # 1. 事件总是发 (audit 路径与具体 action 无关)
        notice = self._build_notice(source="input", scan_result=result, action=self._input_action)
        self._emit_blocked(agent_state=hook_input.agent_state, source="input", scan_result=result, message=notice)

        # 2. 按 action 分支
        action = self._input_action
        if action == OnHitAction.MASK:
            new_messages = [self._mask_message(m) or m for m in hook_input.messages]
            return HookResult(messages=new_messages)

        if action == OnHitAction.TERMINATE:
            new_messages = list(hook_input.messages)
            new_messages.append(Message(role=Role.ASSISTANT, content=[TextBlock(text=notice)]))
            return HookResult(messages=new_messages, force_stop_reason=AgentStopReason.ERROR_OCCURRED)

        # SOFT_REJECT: 跳过本轮 LLM 调用 + 用温和拒绝文案; terminal_reason=SUCCESS 不是错误。
        # 同时也 mask 历史里命中的敏感词, 避免下一轮 (用户换话题) 时 history 残留的
        # 同一敏感词又触发 soft_reject 导致"永远聊不下去"。语义: 礼貌跳过当轮, 后续可继续。
        new_messages = [self._mask_message(m) or m for m in hook_input.messages]
        new_messages.append(Message(role=Role.ASSISTANT, content=[TextBlock(text=notice)]))
        return HookResult(messages=new_messages, force_stop_reason=AgentStopReason.SUCCESS)

    def after_model(self, hook_input: AfterModelHookInput) -> HookResult:  # type: ignore[override]
        """Scan visible model output after the LLM call; act per ``output_action`` on hit.

        RFC-0027: 输出侧拦截

        只扫可见正文（``original_response``），不动 reasoning。
        terminate: 用拒绝文案替换违规末条 assistant 消息 + force_stop_reason=ERROR_OCCURRED。
        mask: 把命中位置在 assistant 文本内 mask 掉, run 继续。
        soft_reject: 用温和拒绝文案替换违规 assistant 消息 + force_stop_reason=SUCCESS。
        """
        if not self._block_output or self._lexicon_size == 0:
            return HookResult.no_changes()

        text = hook_input.original_response or ""
        result = self.scan_text(text)
        if not result.matched:
            return HookResult.no_changes()

        if self._raise_on_block:
            raise SensitiveContentBlockedError(source="output", scan_result=result)

        notice = self._build_notice(source="output", scan_result=result, action=self._output_action)
        self._emit_blocked(agent_state=hook_input.agent_state, source="output", scan_result=result, message=notice)

        action = self._output_action
        messages = list(hook_input.messages)

        if action == OnHitAction.MASK:
            # 直接对末条 assistant 做 mask (executor 已把违规 assistant 追加为末条)
            if messages and messages[-1].role == Role.ASSISTANT:
                masked = self._mask_message(messages[-1]) or messages[-1]
                messages[-1] = masked
            return HookResult(messages=messages)

        # TERMINATE / SOFT_REJECT 都用 notice 替换违规 assistant, 区别在 stop reason
        replacement_msg = Message(role=Role.ASSISTANT, content=[TextBlock(text=notice)])
        if messages and messages[-1].role == Role.ASSISTANT:
            messages[-1] = replacement_msg
        else:
            messages.append(replacement_msg)
        stop_reason = AgentStopReason.ERROR_OCCURRED if action == OnHitAction.TERMINATE else AgentStopReason.SUCCESS
        return HookResult(messages=messages, force_stop_reason=stop_reason)

    # ------------------------------------------------------------------
    # 拒绝文案构造
    # ------------------------------------------------------------------

    def _build_notice(
        self,
        *,
        source: str,
        scan_result: SensitiveScanResult,
        action: OnHitAction,
    ) -> str:
        """Render the user-visible notice for a hit. Template depends on ``action``.

        terminate → refusal_template (严厉拒绝)
        soft_reject → soft_refusal_template (温和告知)
        mask → soft_refusal_template (虽然内容继续, 但事件 message 字段仍用同款文案)
        """
        # 1. 命中词 / 类别 preview（避免泄漏全文）
        hits_preview = ", ".join(scan_result.words[:5])
        if len(scan_result.words) > 5:
            hits_preview += f" … (+{len(scan_result.words) - 5} more)"
        category_preview = "/".join(scan_result.categories[:3]) or "unknown"

        # 2. 审计日志
        logger.warning(
            "[SensitiveWordMiddleware] HIT source=%s action=%s categories=%s hits=[%s]",
            source,
            action.value,
            category_preview,
            hits_preview,
        )

        # 3. 选模板
        if action == OnHitAction.TERMINATE:
            template = self._refusal_template
        else:
            template = self._soft_refusal_template

        return template.format(
            source="用户输入" if source == "input" else "模型输出",
            category=category_preview,
            hits=hits_preview,
        )

    # ------------------------------------------------------------------
    # 事件发射
    # ------------------------------------------------------------------

    def set_event_emitter(self, emitter: Callable[[object], None]) -> None:
        """Receive the unified event emitter from the executor.

        RFC-0027: executor 的 _wire_middleware_event_emitters 会把统一事件回调
        注入进来（仅当链路中存在 on_event 提供方，如 AgentEventsMiddleware）。
        """
        self._event_emitter = emitter

    def _emit_blocked(
        self,
        *,
        agent_state: AgentState,
        source: Literal["input", "output"],
        scan_result: SensitiveScanResult,
        message: str,
    ) -> None:
        """Emit a dedicated ContentBlockedEvent when content is blocked.

        RFC-0027: 命中即时上报专属的内容安全拦截事件（携带来源/类别/命中词）。
        与终止用的 ``RunErrorEvent`` 区分；停止原因复用 ``ERROR_OCCURRED``。
        无 emitter（未装载事件中间件）时静默跳过。
        """
        if self._event_emitter is None:
            return
        self._event_emitter(
            ContentBlockedEvent(
                run_id=agent_state.run_id,
                source=source,
                categories=scan_result.categories,
                words=scan_result.words,
                message=message,
            )
        )


__all__ = [
    "OnHitAction",
    "SensitiveContentBlockedError",
    "SensitiveHit",
    "SensitiveScanResult",
    "SensitiveWordMiddleware",
]
