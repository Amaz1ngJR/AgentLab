# AgentLab

运行在个人电脑上的本地 Agent 应用。支持对话、读写文件、执行命令、维护任务清单，可在本地模型（Ollama / Qwen 等）和云端模型（Claude / GPT）之间切换，只改配置不改代码。

## 功能

- **对话**：多轮上下文对话；流式输出文本逐字展示，token 实时增长
- **文件操作**：read_file / write_file / list_dir，受 `WORKSPACE_ROOT` 限制
- **Shell 命令**：跨平台 shell 工具（Mac/Linux 用 bash，Windows 用 PowerShell），cwd 锁定 workspace
- **任务面板**：模型用 todo_write 维护多步任务清单，CLI 实时显示进度（`✓ done` / `❯ in_progress` / `○ pending`）
- **方向键审批**：写操作 / shell 命令前弹出菜单 `允许这次 / 总是允许 / 拒绝`，↑↓ 选择 + Enter
- **模型切换**：`--profile` 或 `.env` 切换本地 / 云端，不改代码；本地端点不通时给出可执行修复指引
- **进度可见**：LLM 调用期间 `✻ thinking… (3.2s · ↓ 42 tokens)` 实时刷新；每轮末尾打印耗时与 token

## 安装

```bash
conda activate myenv   # 或自建 venv
pip install -r requirements.txt
cp .env.example .env
```

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

▸ 帮我重构 cli.py 里的 spinner 逻辑

  4 tasks (1 done, 1 in progress, 2 open)
    ✓ 读取并理解 cli.py 当前实现
    ❯ 找出可重构的点
    ○ 改写 spinner
    ○ 跑测试

  ✻ thinking… (3.4s · ↓ 87 tokens)
  · tool read_file({"path": "app/cli.py"})
    [ok] (1ms) ...

[流式中文回复 + 后续工具循环]

  [stats] turn 12.3s in=1234 out=456 | session 12.3s in=1234 out=456
```

写文件 / 执行 shell 命令时会弹出方向键菜单：

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

## 配置

唯一入口：`.env` + `config/models.yaml`。

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

## 安全

- **路径限制**：所有文件工具受 `WORKSPACE_ROOT` 约束，越界返回 `refused: ...` 给模型，不抛异常
- **写操作审批**：`write_file` / `shell` 等 `requires_approval=True` 的工具默认弹方向键菜单确认；`-y` 跳过
- **凭据脱敏**：异常 traceback 输出前用正则脱敏 `Bearer xxx` / `sk-ant-xxx` / `cr_xxx` / `x-api-key=xxx` 等

## 目录结构

```
AgentLab/
├── app/
│   ├── cli.py              # CLI 入口 + spinner + 任务面板渲染
│   ├── config/             # 配置加载（profile + .env）
│   ├── models/             # Anthropic / OpenAI Responses / OpenAI-compatible 三种 adapter
│   ├── agent/              # AgentSession 多轮工具循环 + 审批策略 + TaskStore
│   ├── tools/builtin/      # files / shell / todo_write 内置工具
│   └── util/               # 方向键菜单 + 凭据脱敏
├── config/
│   ├── models.yaml         # 模型 profile 注册表
│   └── app.example.yaml    # 应用配置模板
├── scripts/
│   ├── install_local_model.sh   # macOS / Linux 一键安装 Ollama + 模型
│   ├── install_local_model.ps1  # Windows 同上
│   └── verify_local_model.sh    # 端到端 5 步验证
├── docs/
│   ├── technical_architecture.md  # 系统设计方案
│   ├── local_model_guide.md       # 本地模型落地指南
│   └── process.md                 # 开发进度与下一步计划
├── tests/unit/             # 离线单元测试（78 个）
└── .env.example
```

## 开发

```bash
python -m pytest tests/unit/ -v
```

进度与路线见 [`docs/process.md`](docs/process.md)；总体设计方案见 [`docs/technical_architecture.md`](docs/technical_architecture.md)。
