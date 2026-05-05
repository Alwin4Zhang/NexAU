# 2026-05-04 — Live test economics: what must be live, what should be recorded, how to assert without flake

**TL;DR**: 给 RFC-0023 PR-C.2 之后的 81 个 SKIPPED live 测试连线时，最初
的 "全连上每 PR 跑" 方案让 test-saas 时长涨到 ~15min、test 成本明显上去、
还间歇 false-fail。重新分类后总结了一套 **live vs replay 决策原则** + 一套
**cache 命中断言的不变量写法** + 一套 **per-PR / nightly 分层 + 单一名单**
机制。本文是给后续 agent / 同事的导航：写新 LLM 测试时先翻这里。

**Date**: 2026-05-04
**Driver**: PR #519 (closes #518) — wiring live cross-provider matrix
**Files**:
- `tests/aggregator_parity/fixtures/<provider>/recordings/*.sse` (cassettes)
- `tests/integration/test_aggregator_live_e2e.py` (live smoke + nightly)
- `tests/integration/test_block_matrix_langfuse_live.py` (nightly only)
- `tests/integration/test_token_usage_live_matrix.py` (nightly only)
- `tests/integration/test_two_turn_payload_live.py` (per-PR cache + signature)
- `tests/conftest.py` (`_AGGREGATOR_LIVE_SMOKE`, `live_nightly` auto-marker)
- `pytest.ini` (`live_nightly` marker registration)
- `Makefile` (`test` excludes nightly, `test-nightly` runs only nightly)
- `.github/workflows/nightly.yml`
- `scripts/notify_lark.sh`

---

## 决策树：新写一个 LLM 相关测试时先问

```
1. 这个测试在验证什么？
   ├── (A) 我们的 SDK 处理 wire format 正确（aggregator / 字段提取 / 流拼接）
   │   → REPLAY: 录一份 SSE 进 fixtures/<provider>/recordings/，加进
   │     test_set_a_aggregator_replay.py 的断言
   │
   ├── (B) 真实跨轮 / 跨 provider 行为（cache prefix 稳定、thinking signature
   │     回传、跨 provider gap、auth/TLS、真实 SSE chunk 时序）
   │   → LIVE: 但要分类
   │     ├── 必须每 PR 抓: cache invariant (cost-impacting), signature roundtrip
   │     │   → per-PR (放进 _AGGREGATOR_LIVE_SMOKE 或 same-provider 双轮)
   │     └── drift 检测: cross-provider matrix, 各种 quirky tools / multichunk
   │         → live_nightly (只在 nightly job 跑)
   │
   └── (C) 仅仅是覆盖率 / "跑通就算" → 想想是否真有信号；多半可以删
```

**默认偏好 replay**。Live 必须有具体回答："录制为什么不够？"

---

## 真正必须 live 的四类

| 维度 | 为什么录制不行 | 现在归属 |
|------|--------------|---------|
| **prompt cache 稳定性** | 命中/失效取决于 provider 实时缓存状态；录制只能定格一次结果，没法验证"我们的 prefix 结构能持续命中" | per-PR `test_two_turn_payload_live` |
| **Anthropic thinking signature 跨轮回传** | signature 是 provider 端签名/会话绑定；录回放，第 2 轮发回去 provider 判 stale；只有 live → live 能确认接受 | per-PR `test_two_turn_payload_live` |
| **跨 provider gap** | A 的 reasoning_summary → B、A 的 tool_call.id → B；录制能捕捉 A 输出 + B 回应一次，但每次 PR 要验证 "B 当前是否仍接受我们构造的 payload"，回放只是回放历史接受 | nightly `test_block_matrix_langfuse_live` (64-case) |
| **真实流式时序 / 网络层** | SSE 录制只剩字节序列，时序丢失；auth/TLS/gateway 路由触不到 | per-PR 4 个 smoke + nightly drift |

其他全部 → replay。

---

## Cache 命中断言的正确写法（关键陷阱）

### 错的写法（最初做的）

```python
cached = response.usage.prompt_tokens_details.cached_tokens
assert cached > 0, "cache miss"
```

**为什么错**：把两件事混在一起：
1. **CI runner 第一次见到这个 system 内容** → 全局 cache cold → cached=0（不是我们 bug）
2. **SDK 真的破坏了前缀** → cache miss（这才是要抓的）

第 1 种在 CI 上每次都可能触发（prompt 改了一个字符，gateway 没缓存过），
变成稳定 false-fail。

### 对的写法（不变量式）

```python
cached_1 = _safe_cached(resp1.usage, "prompt_tokens_details")
cached_2 = _safe_cached(resp2.usage, "prompt_tokens_details")
assert cached_2 >= cached_1, "turn-2 cache regressed below turn-1"
```

**为什么对**：turn 2 的可缓存前缀是 turn 1 的**严格超集**。无论全局 cache
冷热，只要我们 SDK 没破坏前缀，都满足 `turn2.cached >= turn1.cached`：

| 场景 | 旧 `> 0` | 新 `>= turn1` |
|------|---------|--------------|
| Cache 双 cold（CI 首次） | ❌ 误报 | ✅ 0 ≥ 0 |
| Cache 双 warm | ✅ | ✅ |
| **SDK 破坏前缀**（真 bug） | ⚠️ 漏报 | ❌ 抓到 |

新断言反而更紧 —— 旧的会漏抓 "turn 1 命中但 turn 2 miss"（说明 turn 2
prefix 走偏了）这种回归。

### 还有：usage 字段可能整个为 None

```python
def _safe_cached(usage: Any, details_attr: str) -> int:
    """Return ``usage.<details_attr>.cached_tokens`` or 0."""
    details = getattr(usage, details_attr, None)
    if details is None:
        return 0
    return getattr(details, "cached_tokens", None) or 0
```

OpenAI 在 cache 完全没参与时可能省略整个 `prompt_tokens_details` 块，
直接 `.cached_tokens` 会 `AttributeError`。defensive 拿。

### 各 provider cache 字段路径

| Provider | 字段 |
|----------|------|
| OpenAI Chat | `usage.prompt_tokens_details.cached_tokens` |
| OpenAI Responses | `usage.input_tokens_details.cached_tokens` (字段名不同！) |
| Anthropic | `usage.cache_read_input_tokens` (扁平，无 details 包装) |
| Gemini | 需 `cachedContent` 资源 API（暂未在 NexAU SDK 中） |

### Anthropic 还要显式 cache_control

```python
llm_kwargs["cache_control_ttl"] = "5m"
# system prompt 必须 ≥ 1024 tokens (sonnet 4-5)
```

否则 Anthropic 直接不缓存（不像 OpenAI 自动缓存）。NexAU SDK 已支持，
通过 `LLMConfig.cache_control_ttl`，会自动 apply 到 system block。

---

## Per-PR vs Nightly 分层

### Per-PR (默认，每次 push 都跑)

- 全部 replay 测试（aggregator parity, set A replay, ump matrix）
- 4 个 per-provider live smoke (一个 provider 一个最简 streaming)
- `test_two_turn_payload_live` (cache + signature)
- 所有 unit + windows + e2e

CI 时长目标：≤10min

### Nightly (`live_nightly` marker, cron 02:00 CST)

- 64-case `test_block_matrix_langfuse_live` (cross-provider drift)
- 41-case `test_token_usage_live_matrix` (token usage drift)
- 23 个 `test_aggregator_live_e2e` 中非 smoke 的（tools / multichunk /
  reasoning / async / shutdown / traced）

失败发 Lark，**不阻塞合并**（drift 不是事故，第二天处理就行）。

### 如何把测试归类

**单一名单原则**：don't decorate 23 个测试每个加 `@pytest.mark.live_nightly`。
改成在 `tests/conftest.py` 里维护一个 SMOKE 允许名单：

```python
_AGGREGATOR_LIVE_SMOKE = frozenset({
    "test_openai_chat_streaming_e2e_northgate_gpt52",
    "test_openai_responses_streaming_e2e_northgate_gpt52",
    "test_anthropic_streaming_e2e_northgate_sonnet45",
    "test_gemini_rest_streaming_e2e_gateway_31pro",
})

# in pytest_collection_modifyitems:
if "test_aggregator_live_e2e.py" in str(item.fspath) and item.name not in _AGGREGATOR_LIVE_SMOKE:
    item.add_marker(pytest.mark.live_nightly)
```

所有未列名的自动 nightly。**删测试 / 加测试时只动一个 frozenset**，不会
和 decorator 漂移。

对于整文件都是 nightly 的（`test_block_matrix_*`, `test_token_usage_*`），
直接在文件顶部 `pytestmark = [..., pytest.mark.live_nightly]`。

### Make 入口

```bash
make test           # excludes -m "live_nightly"
make test-nightly   # only -m "live_nightly"
```

---

## 录制 SSE 的具体步骤

```bash
# 1. 直接 curl，输出重定向到 .sse 文件
curl -sS -N https://gateway/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{...payload...}' > tests/aggregator_parity/fixtures/<provider>/recordings/<scenario>.sse

# 2. 验证录制能 parse + 通过 aggregator
uv run pytest tests/unit/test_set_a_aggregator_replay.py::test_all_oac_recordings_parse_and_build -v

# 3. 加针对性断言（mirror 已有的 test_openrouter_reasoning_details_preserved 风格）
```

录制内容是 raw SSE bytes，跟 `curl -N` 输出一字不差。SDK 用 `_parse_sse_blocks`
拆出 events 喂给 aggregator。

**录制不需要 redact**：API key 在 request header 不在 response body；
session id 等 metadata 字段对 replay 测试无影响。

---

## CI 通知机制

### 共享 sender

`scripts/notify_lark.sh "<message>"` —— 自动签名（HMAC-SHA256），缺
`LARK_WEBHOOK` env 时 warn-and-exit（不阻塞 CI）。

### 三个 workflow

| Workflow | 触发 | 内容 |
|----------|------|-----|
| `nightly.yml` | cron 18:00 UTC + manual | drift 失败时通知 |
| `main-failure-alert.yml` | `workflow_run` of CI | main 分支 push 后 CI 失败：commit + 作者 + 失败 job |
| `pr-stuck-alert.yml` | `workflow_run` of CI | PR 24h 内 ≥3 次失败：通知作者 + PR comment 去重 |

PR-stuck 用 `<!-- pr-stuck-alert -->` sentinel comment 防止重复通知；
作者删 comment = re-arm。

### Secrets

- `LARK_NIGHTLY_WEBHOOK` (URL)
- `LARK_NIGHTLY_SIGN_SECRET` (HMAC-SHA256 key)

新 channel 加新 webhook 即可，sender 脚本通用。

---

## 教训 / 反模式

### "把所有 live 都连上每 PR 跑" 是错的

直觉是 "信号越多越好"，实际：
- 时长涨到 ~15min（开发体验差）
- LLM 调用成本线性涨
- false-fail 噪音淹没真信号
- **Drift 检测 vs 回归检测分不清**

正确：先问 "这个测试是抓我们的 bug 还是 provider 的 drift？"。

### 只检 trace count 不检字段是 eventual consistency 陷阱

Langfuse / 任何 async ingest backend：trace 出现在列表 ≠ 字段已索引完。
fetch helper 必须 retry 直到关键字段非空（见 `require_named=True`），
否则 `flush()` 后立即查询会 race。

### Cache 断言用绝对值是错的

详见上面 "Cache 命中断言的正确写法"。**不变量式断言** (turn2 ≥ turn1)
比 **绝对值断言** (cached > 0) 既更准（抓回归）又更稳（不 false-fail）。

### 测试 mock async 客户端时不能 patch sync slot

```python
# Agent.run() 内部 → asyncio.run(run_async) → 用 _async_openai_client
# Patch agent.openai_client (sync) 是 no-op
# 必须 patch agent._async_openai_client
```

5 周前这个 bug 让 `test_two_turn_payload_live` 一直 "通过" 但 spy.calls
全空 —— 假验收典型。改 sync → async + 改 patch target 才真正激活了断言。

### deepseek-v4-flash 是早期 placeholder，不是契约

Smoke 测试选 model 看的是 "是否覆盖目标 wire format + 不会跟 provider 状态
强绑定"。最初选 deepseek-v4-flash 是因为它便宜，但 northgate 的 deepseek
上游余额会枯竭，并且不支持 vision。后来切到 `gpt-5.2`：覆盖 OpenAI Chat
shape 同样的目的，且 northgate 的 gpt 路由独立计费、相对稳定。

教训：**smoke target 的选择是可演化的**。当上游 quirk 出现时换 model
而不是修测试。
