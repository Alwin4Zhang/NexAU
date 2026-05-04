# Claude Code Development Guide

This is the entry point for understanding NexAU's implementation. For user documentation on how to use NexAU, see [docs/](./docs/).

## Quick Reference

### Module Implementation Guides

For detailed implementation guides for each module, see:

- **[Agent System](./nexau/archs/main_sub/CLAUDE.md)** - Agent container, executor, middleware
- **[Tool System](./nexau/archs/tool/CLAUDE.md)** - Tool definition, execution, binding
- **[Session Management](./nexau/archs/session/CLAUDE.md)** - ORM, repositories, history persistence
- **[Transport System](./nexau/archs/transports/CLAUDE.md)** - HTTP, stdio, WebSocket, gRPC
- **[LLM Aggregators](./nexau/archs/llm/llm_aggregators/CLAUDE.md)** - Event streaming, providers
- **[Cross-Platform Guidelines](./docs/cross-platform-guidelines.md)** - Platform abstraction, path handling, degradation strategy (RFC-0019/0020)

### User Documentation

- **[Documentation Index](./docs/index.md)** - Main user-facing documentation
- **[Getting Started](./docs/getting-started.md)** - Installation, setup, first agent
- **[Core Concepts](./docs/core-concepts/)** - Agents, Tools, LLMs
- **[Advanced Guides](./docs/advanced-guides/)** - Skills, Hooks, Tracing, MCP

## Development Commands

This project uses **uv** for package management and **Make** for workflow orchestration. Python 3.12+ is required.

```bash
# Install dependencies and pre-commit hooks (first time setup)
make install

# Format code (auto-fix)
make format

# Run linter (auto-fix mode)
make lint

# Run all type checking (mypy + pyright)
make typecheck

# Run tests with coverage
make test

# Full CI pipeline locally
make ci
```

### Running Individual Tests

```bash
# Run a single test file
uv run pytest path/to/test_file.py

# Run a specific test function
uv run pytest path/to/test_file.py::test_function_name

# Run with verbose output
uv run pytest path/to/test_file.py -v

# Run tests without coverage (faster)
uv run pytest path/to/test_file.py --no-cov
```

### Type Checking

```bash
# Run mypy only
make mypy

# Run pyright only
uv run pyright

# Generate type coverage reports
make mypy-coverage
# Reports saved to: mypy_reports/type_html/index.html
```

## Aggregator parity protocol(LLM stream aggregators)

NexAU currently maintains **two parallel implementations** of LLM stream
aggregation per provider, and they MUST stay in lock-step until RFC-0023
§阶段 ③ retires the second set:

| Set | Path | Output |
|-----|-----|--------|
| Set A | `nexau/archs/llm/llm_aggregators/<provider>/` | unified Event objects (push to UI via SSE) |
| Set B | `nexau/archs/main_sub/execution/llm_caller.py` `*StreamAggregator` classes | ModelResponse dict (persist to history) |

**Every LLM call goes through both** — they parse the same provider SSE
stream independently. Drift between them causes user-visible bugs:
the live SSE shows one thing, but the persisted message history shows
another.

### When to engage the parity harness

You MUST run `uv run pytest tests/aggregator_parity/` before commit if
your change touches:

- Any file under `nexau/archs/llm/llm_aggregators/{anthropic,openai_chat_completion,openai_responses,gemini_rest}/`
- Any `*StreamAggregator` class in `nexau/archs/main_sub/execution/llm_caller.py`
- The `Aggregator` ABC or its `Event` union in `llm_aggregators/events.py`

If parity surfaces a divergence:

1. **First instinct: fix the buggy side, do NOT just xfail.** Real Set
   A ↔ Set B drift = real production bug. The harness has already
   caught 5 such bugs that would otherwise have shipped silently
   (see `docs/development/case-studies/2026-05-02-aggregator-parity-harness.md`).
2. If the divergence requires a design decision (e.g. server_tool_use
   shape), THEN xfail with a detailed reason in
   `KNOWN_DIVERGENT_FIXTURES` and open a follow-up issue / RFC.
3. Run the existing aggregator unit tests
   (`tests/unit/test_*_aggregator.py`) too — those still need to pass.

### When to record a new fixture

Add a recording when your change handles a new wire pattern:

- New event type / block type / part type emitted by a provider
- New extension field (e.g. OpenRouter `reasoning_details`, Anthropic
  `thoughtSignature`, Gemini `inlineData`)
- New tool-result variant (e.g. Anthropic `web_search_tool_result`)
- New error / refusal / cancellation pattern

Use the recording script (no manual curl):

```bash
export NEXAU_PARITY_BASE_URL="https://your-gateway.example.com"
export NEXAU_PARITY_API_KEY="sk-..."   # never written to any file

python tests/aggregator_parity/scripts/record_fixture.py \
    --provider anthropic --model claude-sonnet-4-5-20250929 \
    --scenario my_new_scenario \
    --prompt "your test prompt"
```

The script handles SSE streaming, redaction, key-leak scanning, and
fixture path conventions. After it runs, register the new fixture in
`tests/aggregator_parity/fixtures/<provider>/__init__.py`.

### Why this matters

The parity harness has caught 3 production bugs and 2 test-infrastructure
bugs that all map onto industry-wide LLM streaming pathology:

- **Anthropic orphan `thinking_delta`** — same class of bug as Spring
  AI #4407 / LiteLLM #25321
- **OpenAI Responses silent reasoning** — same as koog #1264 /
  multiple OpenAI Community threads
- **`reasoning_content` vs `reasoning` vs `reasoning_details` fragmentation** —
  vLLM RFC-27755 confirmed industry-wide
- **Gemini block-ordering on thinking → tool transition** — fixed by
  closing thinking before opening next block

Skipping the parity harness means re-discovering these bugs the hard
way (production user reports). See
`tests/aggregator_parity/README.md` for full documentation including
the cross-validation table.

## Type Safety Guidelines

This section documents the **mandatory type safety coding standards** that apply across the codebase, especially in `llm_aggregators` module. These rules establish a strict type safety culture.

### Core Principles

1. **Type Checkers Are Your Friends**
   - Type checker errors = actual code problems
   - Suppressing errors only delays runtime failures
   - Refactor instead of ignore

2. **Zero Tolerance Policy**
   - ❌ No `# type: ignore` comments
   - ❌ No `Any` type usage
   - ❌ No dynamic attribute access (`getattr`/`hasattr`)

3. **Documentation Through Types**
   - Type annotations are the best documentation
   - Clear types make code understandable
   - Type checkers catch common errors

### Forbidden Patterns

#### 1. Never Use `# type: ignore`

```python
# ❌ Disables type checking entirely
self._on_event(ThinkingTextMessageStartEvent())  # type: ignore

# ✅ Use TYPE_CHECKING for import cycles
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from some_module import EventType

event: EventType = get_typed_event()
```

#### 2. Never Use `Any`

```python
# ❌ Loses all type safety
def process_data(data: Any) -> Any:
    return data.process()

# ✅ Use TypeVar for generics
from typing import TypeVar
T = TypeVar('T')
def process_data(data: T) -> T:
    return data

# ✅ Use Union for limited types
from typing import Union
def process_event(event: Union[TextEvent, AudioEvent]) -> None:
    match event:
        case TextEvent(): handle_text(event)
        case AudioEvent(): handle_audio(event)
```

#### 3. No Dynamic Attribute Access

```python
# ❌ getattr and hasattr are not type-safe
value = getattr(obj, "name", default)
if hasattr(item, "name") and item.name:
    process(item.name)

# ✅ Use type guards + match statement
from typing import TypeGuard

def is_event_with_name(item: ResponseStreamEvent) -> TypeGuard[EventWithName]:
    return hasattr(item, "name") and isinstance(item.name, str)

if is_event_with_name(item) and item.name:
    process(item.name)

# ✅ Use pattern matching
match item:
    case EventWithName(name=name) if name:
        process(name)
```

#### 4. No String-Based Type Narrowing

```python
# ❌ Bypassing type system
item_type_str = str(item.type)
if item_type_str == "response.reasoning_summary_text.delta":
    # ...

# ✅ Direct literal comparison (type checker can infer)
if item.type == "response.reasoning_summary_text.delta":
    # ...

# ✅ Use pattern matching (most elegant and safe)
match item.type:
    case "response.reasoning_summary_text.delta":
        # ...
```

### Type Safety Checklist

Before committing code, verify:

- [ ] No `# type: ignore` comments
- [ ] No `# type: ignore[...]` specific error suppression
- [ ] No `Any` type usage
- [ ] All functions have complete type signatures
- [ ] No `getattr`/`setattr` for attribute access
- [ ] All properties have explicit type declarations
- [ ] IDE type checker shows no errors (warnings OK)
- [ ] Using type guards instead of dynamic checks
- [ ] Using pattern matching for type handling
- [ ] Run `make typecheck` before committing

## Debugging Tips

### Enable Logging

The framework uses Python logging throughout:

```python
import logging
logging.basicConfig(level=logging.INFO)
# Or DEBUG for more verbosity
```

### Common Issues

- **Import errors**: Ensure `uv sync` ran successfully
- **Type errors**: Run `make typecheck` to find issues
- **Tool call failures**: Check tool schema matches implementation signature
- **Token limit exceeded**: Configure context compaction middleware
- **Sub-agent not found**: Verify agent name matches config key
- **Session storage issues**: Check database permissions and path
- **Transport connection errors**: Verify port availability and network configuration

## RFC convention for NexAU
### 1. 模块级文档 (module docstring)

用于文件顶部，描述整个模块的功能：

```python
# Artifact management routes.
#
#  RFC-0011: Artifact Management APIs
#
#  Provides endpoints for:
#  - Upload artifact (proxy upload through Backend)
#  - Download artifact (proxy download through Backend)
#
#  # Upload Modes
#
#  ## Mode 1: Proxy Upload (Default)
#  1. Client sends `PUT /api/versions/{id}/artifact` with artifact data
#  2. Backend reads data, uploads to S3
```

### 2. 函数/类文档 (docstring)

第一行英文简述，第二行 RFC 引用（中文），后续详细说明：

```python
async def upload_artifact(version_id: str, body: bytes) -> UploadResult:
    """Upload artifact via proxy.

    RFC-0011: 代理上传制品 API

    将上传数据读入内存后上传到 S3。
    同时从 tar 包中提取 nexau.json 作为版本的 config。

    Request::

        PUT /api/versions/{version_id}/artifact
        Content-Type: application/x-tar

    Response (200 OK)::

        { "version_id": "uuid", "uploaded": true }
    """
```

### 3. 函数内部步骤注释 (`#`)

使用编号步骤，中文说明每个逻辑块：

```python
async def upload_artifact(version_id: str, body: bytes) -> UploadResult:
    # 1. 验证版本存在且 artifact 未上传
    version = await get_version(db, version_id)

    # 2. 获取内容大小
    content_length = len(body)

    # 3. 检查文件大小限制 (500MB)
    MAX_ARTIFACT_SIZE = 500 * 1024 * 1024

    # 4. 从 tar 包中提取 nexau.json 配置
    config = extract_config_from_tar(body)

    # 5. 上传到 S3
    artifact_url = await storage.upload(key, body)
```

### 4. 路由注释

在路由定义处标注 RFC 和中文说明：

```python
router = APIRouter()

# RFC-0011: 代理上传制品
@router.put("/{version_id}/artifact")
async def upload_artifact(...): ...

# RFC-0011: 获取制品元数据
@router.get("/{version_id}/artifact")
async def get_artifact_metadata(...): ...
```

### 5. 字段文档

数据类/Pydantic model 字段使用注释或 `Field` 说明：

```python
@dataclass
class GetUploadUrlRequest:
    """Request for getting a pre-signed upload URL."""

    content_type: str = "application/x-tar"
    """Expected content type (default: application/x-tar)"""

    content_length: int | None = None
    """Expected content length in bytes (optional, for validation)"""
```

## RFC 生成规范

当进行功能规划（Plan Mode）时，必须为重要变更生成 RFC 文档：

### 何时需要生成 RFC

- 新增 API 端点或修改现有 API
- 架构变更或新增服务
- 数据模型变更
- 安全相关变更
- 跨服务的功能实现

### RFC 生成流程

1. **参考模板**: 使用 `docs/rfcs/0000-template.md` 作为模板
2. **编号规则**: 查看 `docs/rfcs/` 目录下现有 RFC，使用下一个可用编号（如 `0023`）
3. **文件命名**: `docs/rfcs/XXXX-feature-name.md`（使用短横线分隔的小写英文）
4. **必填字段**:
   - 状态: `draft`（初始状态）
   - 优先级: P0-P3
   - 影响服务: 列出所有受影响的服务
   - 创建日期: 当天日期

### RFC 模板结构

```markdown
# RFC-XXXX: 标题

- **状态**: draft
- **优先级**: P0 | P1 | P2 | P3
- **标签**: `security`, `architecture`, `performance`, `dx` 等
- **影响服务**: 列出受影响的服务
- **创建日期**: YYYY-MM-DD
- **更新日期**: YYYY-MM-DD

## 摘要
一段话描述这个 RFC 要解决什么问题。

## 动机
为什么需要这个变更？当前存在什么问题？

## 设计
### 概述
### 详细设计
### 示例

## 权衡取舍
### 考虑过的替代方案
### 缺点

## 实现计划
### 阶段划分
### 相关文件

## 未解决的问题

## 参考资料
```

### 注意事项

- RFC 应在实现前完成并获得确认
- 实现完成后将状态更新为 `implemented`
