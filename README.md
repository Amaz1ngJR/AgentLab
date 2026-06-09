# AgentLab

运行在个人电脑上的本地 Agent 应用。支持对话、读写文件、搜索代码、执行命令、维护任务清单、操作浏览器、多 Agent 会话与长期记忆，可在本地模型（Ollama / Qwen 等）和云端模型（Claude / GPT）之间切换，只改配置不改代码。

## 功能

- **对话**：多轮上下文对话；流式输出文本逐字展示，token 实时增长
- **文件操作**：read_file / write_file / list_dir，受 `WORKSPACE_ROOT` 限制
- **代码搜索**：`code_search` 支持 text / regex / file / symbol 四种模式，优先用 ripgrep，无 rg 时 Python fallback；遵守 `.gitignore`、命中行密钥脱敏
- **Shell 命令**：跨平台 shell 工具（Mac/Linux 用 bash，Windows 用 PowerShell），cwd 锁定 workspace
- **浏览器控制**：通过 Playwright MCP 打开网页、截图、读 DOM、点击、输入（默认禁用，需显式启用）
- **多 Agent / 会话**：`/session` 命令族在一个进程里创建、切换、归档多个 Agent 会话；会话消息存 SQLite，可恢复
- **长期记忆**：按 AgentProfile 的 `memory_policy` 检索历史记忆注入上下文，`read_write` 策略会话结束写摘要
- **任务面板**：模型用 todo_write 维护多步任务清单，CLI 实时显示进度（`✓ done` / `❯ in_progress` / `○ pending`）
- **方向键审批**：写操作 / shell 命令 / 浏览器动作前弹出菜单 `允许这次 / 总是允许 / 拒绝`，↑↓ 选择 + Enter
- **模型切换**：`--profile` 或 `.env` 切换本地 / 云端，不改代码；本地端点不通时给出可执行修复指引
- **进度可见**：LLM 调用期间 `✻ thinking… (3.2s · ↓ 42 tokens)` 实时刷新；每轮末尾打印耗时与 token

## 安装

需要 **Python 3.11+**（MCP 功能依赖；纯对话/文件/shell 用 3.9+ 也可，但推荐 3.11）。

```bash
conda create -n agentlab python=3.11   # 或自建 venv
conda activate agentlab
pip install -r requirements.txt
cp .env.example .env
```

浏览器控制还需要 Node/npx（首次启动会自动 `npx` 下载 Playwright）。

## 快速开始

### 选项 A：用 Anthropic Claude（推荐起步，凭代理 token 即可）

编辑 `.env`：

```env
ACTIVE_PROFILE=cloud_claude
ANTHROPIC_AUTH_TOKEN=cr_xxxxxxxx
ANTHROPIC_BASE_URL=https://your-proxy/api/
```

```bash
python -m app
```

### 选项 B：用本地 Qwen2.5-Coder 7B（完全离线）

```bash
# 一键安装(检测/启动 Ollama + 下载 Qwen2.5-Coder 7B + 验证可推理)
bash scripts/install_local_model.sh                 # macOS / Linux
# Windows: powershell -ExecutionPolicy Bypass -File scripts\install_local_model.ps1

# 切到本地 profile
echo "ACTIVE_PROFILE=local_qwen" > .env
python -m app

# 端到端验证(连通 + 工具调用 + 任务面板)
bash scripts/verify_local_model.sh
```

模型选型 / 硬件评估 / 局域网 GPU 模式见 [`docs/local_model_guide.md`](docs/local_model_guide.md)。

## 交互示例

```
== AgentLab ==
provider : anthropic
model    : claude-sonnet-4-6
profile  : cloud_claude
能力     : chat, tools
workspace: /Users/you/AgentLab
工具     : read_file / write_file / list_dir / shell / todo_write
输入 /reset 清空会话; /session [list|new|switch|...] 管理多 Agent; exit/quit 退出.

▸ 帮我重构 cli.py 里的 spinner 逻辑

  4 tasks (1 done, 1 in progress, 2 open)
    ✓ 读取并理解 cli.py 当前实现
    ❯ 找出可重构的点
    ○ 改写 spinner
    ○ 跑测试

  ✻ thinking… (3.4s · ↓ 87 tokens)
  · tool code_search({"query": "spinner", "mode": "symbol"})
    [ok] (12ms) ...
  · tool read_file({"path": "app/cli.py"})
    [ok] (1ms) ...

[流式中文回复 + 后续工具循环]

  [stats] turn 12.3s in=1234 out=456 | session 12.3s in=1234 out=456
```

写文件 / 执行 shell 命令 / 浏览器动作时会弹出方向键菜单：

```
工具: write_file
参数: {"path": "app/cli.py", "content": "..."}

是否允许执行?
❯ 1. 允许这次
  2. 本会话总是允许 write_file
  3. 拒绝

↑↓ 移动 · Enter 确认 · 1-9 快捷键 · Esc 取消
```

## 命令行参数

```bash
python -m app                          # 交互式 REPL
python -m app -p "帮我看 README.md"    # 单次 prompt 后退出
python -m app -y                       # 自动放行所有工具（跳过审批）
python -m app --profile local_qwen     # 临时切换 profile,不改 .env
```

## REPL 内命令

在交互模式里可输入这些斜杠命令（不发给模型）：

| 命令 | 作用 |
|---|---|
| `/reset` | 清空当前会话的消息和任务 |
| `/session` | 显示当前 session 信息 |
| `/session list` | 列出所有活跃 session |
| `/session agents` | 列出 `config/agents.yaml` 里可用的 Agent |
| `/session new [agent_id] [标题]` | 新建并切换到一个 Agent 会话 |
| `/session switch <session_id>` | 切换到已有 session（可从 SQLite 恢复历史） |
| `/session rename <标题>` | 重命名当前 session |
| `/session archive` | 归档当前 session |
| `exit` / `quit` / Ctrl-D | 退出 |

不同 session 的消息历史、任务清单互相隔离，切换时不串。

## 多 Agent 与长期记忆

复制模板启用多 Agent：

```bash
cp config/agents.example.yaml config/agents.yaml
```

`config/agents.yaml` 每个条目定义一个 Agent：

```yaml
agents:
  coder:
    name: 代码助手
    model_profile: cloud_claude     # 用 config/models.yaml 里哪个模型
    system_prompt: |                # 可选,覆盖默认 system prompt
      你是专注代码的助手,优先用 code_search 定位,read_file 精读。
    memory_policy: read_write        # none / read / read_write
    max_steps: 12
```

`memory_policy`：

- `none` — 不使用长期记忆（默认，最安全）
- `read` — 会话开始时检索相关记忆注入上下文，不写入
- `read_write` — 同 read，且会话结束时把对话摘要写入记忆，下次可检索

会话、消息、记忆、工具执行审计存在 `data/agentlab.db`（SQLite，已 gitignore）。

## 浏览器控制（Playwright MCP）

默认关闭。启用后模型可打开网页、截图、读 DOM、点击、输入。

```bash
# 1. 复制模板
cp config/mcp_servers.example.yaml config/mcp_servers.yaml
# 2. 把其中 playwright 的 enabled 改成 true
# 3. 启动(首次 npx 会下载 @playwright/mcp 和浏览器内核)
python -m app
```

工作流：模型先 `browser_navigate` 打开页面 → `browser_snapshot` 拿元素 ref → 用 ref 做 `browser_click` / `browser_type`。点击/输入默认每次需审批，`browser_snapshot` 等只读工具免审批。

**两种 profile**（在 `config/mcp_servers.yaml` 的 args 配置）：

- **隔离 profile**（默认，最安全）：每次新开干净浏览器，不带任何登录态
- **named 持久化 profile**：加 `--user-data-dir data/browser-profiles/<名字>`，首次手动在弹出窗口登录，cookie 落盘后跨会话复用 —— 适合需要登录的内部 wiki / 文档站

> ⚠ **数据边界**：用云端模型 + 浏览器控制时，页面截图 / DOM / 表单内容会发送到云端模型用于推理。named profile 下登录态访问的真实数据同样会进上下文。CLI 启动时会显著提示。需严格隔离请用本地模型或隔离 profile。

> 注：Playwright 用的是自带的 Chromium，不接管你日常的 Chrome/Safari，也不读你日常浏览器的登录态。

## 配置

模型配置入口：`.env` + `config/models.yaml`。

`.env` 选 profile + 提供凭据：

```env
ACTIVE_PROFILE=cloud_claude         # 必填,对应 config/models.yaml 中的 profile

# Anthropic(代理)
ANTHROPIC_AUTH_TOKEN=cr_xxxx
ANTHROPIC_BASE_URL=https://your-proxy/api/

# 限制 Agent 文件工具的访问范围(可选,默认项目根)
WORKSPACE_ROOT=/Users/you/some-project
```

`config/models.yaml` 内置 profile：

| Profile | 说明 | 需要的环境变量 |
|---|---|---|
| `cloud_claude` | Anthropic Claude Sonnet | `ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY` |
| `cloud_claude_opus` | Anthropic Claude Opus | 同上 |
| `cloud_openai` | OpenAI Responses API（GPT 系列） | `OPENAI_API_KEY` |
| `local_qwen` | 本机 Ollama + Qwen2.5-Coder 7B | 无（需先 `ollama pull`） |
| `local_qwen14b` | 本机 Ollama + Qwen2.5-Coder 14B | 无 |
| `local_deepseek` | 本机 Ollama + DeepSeek-R1 7B | 无（不带工具能力） |
| `lan_qwen` | 局域网 GPU 主机 Ollama | 无（修改 profile 里的 `base_url`） |

切换 profile：改 `.env` 中 `ACTIVE_PROFILE`，或临时 `python -m app --profile xxx`。

配置文件分两类（`*.example.yaml` 入库作模板，去掉 `.example` 的真实配置 gitignore 不入库）：

| 模板 | 复制为 | 作用 |
|---|---|---|
| `config/models.yaml` | （直接用） | 模型 profile 注册表 |
| `config/agents.example.yaml` | `config/agents.yaml` | 多 Agent 定义 |
| `config/mcp_servers.example.yaml` | `config/mcp_servers.yaml` | MCP server（如 Playwright） |

## 安全

- **路径限制**：所有文件/搜索工具受 `WORKSPACE_ROOT` 约束，越界返回 `refused: ...` 给模型，不抛异常
- **写操作审批**：`write_file` / `shell` / 浏览器点击输入等默认弹方向键菜单确认；`-y` 跳过
- **凭据脱敏**：异常 traceback、工具输出、记忆写入前用正则脱敏 `Bearer xxx` / `sk-ant-xxx` / `cr_xxx` / `x-api-key=xxx` 等
- **MCP 隔离**：新 MCP server 默认禁用；启用前 CLI 展示 server / transport / 工具列表；只透传 `PATH` 给子进程
- **云端数据边界**：浏览器控制 + 云端模型时启动显著提示页面内容将离开本机
- **登录态保护**：浏览器默认用隔离 profile，不接管日常浏览器；用 named profile 复用登录态需显式配置

## 目录结构

```
AgentLab/
├── app/
│   ├── cli.py              # CLI 入口 + spinner + 任务面板 + SessionRouter 接入
│   ├── config/             # 配置加载（profile + .env）
│   ├── models/             # Anthropic / OpenAI Responses / OpenAI-compatible 三种 adapter
│   ├── agent/              # AgentSession 工具循环 + 审批 + TaskStore + profiles + session_router
│   ├── tools/builtin/      # files / code_search / shell / todo_write 内置工具
│   ├── mcp/                # MCP Client：config / manager(sync↔async 桥) / adapter
│   ├── storage/            # SQLite：sessions / messages / memories / tool_executions
│   ├── memory/             # 长期记忆策略 + 记忆注入
│   └── util/               # 方向键菜单 + 凭据脱敏
├── config/
│   ├── models.yaml                  # 模型 profile 注册表
│   ├── agents.example.yaml          # 多 Agent 定义模板
│   └── mcp_servers.example.yaml     # MCP server 模板（Playwright）
├── scripts/
│   ├── install_local_model.sh   # macOS / Linux 一键安装 Ollama + 模型
│   ├── install_local_model.ps1  # Windows 同上
│   └── verify_local_model.sh    # 端到端 5 步验证
├── docs/
│   ├── technical_architecture.md  # 系统设计方案
│   ├── local_model_guide.md       # 本地模型落地指南
│   └── process.md                 # 开发进度与下一步计划
├── tests/unit/             # 离线单元测试（174 个）
└── .env.example
```

## 开发

```bash
conda activate agentlab
python -m pytest tests/unit/ -v
```

进度与路线见 [`docs/process.md`](docs/process.md)；总体设计方案见 [`docs/technical_architecture.md`](docs/technical_architecture.md)。
