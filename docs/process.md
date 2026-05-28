# AgentLab 开发进度

本文档供 AI 协作时参考：当前已实现什么、接下来要做什么、每个阶段的完成标准。

设计目标见 [`technical_architecture.md`](./technical_architecture.md)，本文件只跟踪进度。

---

## 当前目录结构

```
AgentLab/
├── app/
│   ├── __main__.py
│   ├── cli.py                    # CLI 入口 + spinner + prompt_toolkit 输入框
│   ├── config/
│   │   ├── loader.py             # 配置加载（profile + .env）
│   │   └── schemas.py            # LLMConfig / ModelProfile dataclass
│   ├── models/
│   │   ├── protocol.py           # ToolCall / ModelResponse / ToolResult / 流式回调类型
│   │   ├── anthropic_adapter.py  # Anthropic 流式 adapter（含工具循环）
│   │   ├── compatible_adapter.py # OpenAI-compatible Chat Completions（Ollama 等）
│   │   ├── openai_adapter.py     # OpenAI Responses API（GPT 官方）
│   │   └── router.py             # build_model_router() 工厂
│   ├── agent/
│   │   ├── runtime.py            # AgentSession，多轮工具循环 + 流式事件桥接
│   │   ├── approval.py           # AutoApprove / InteractivePolicy(方向键菜单) / DenyAll
│   │   └── tasks.py              # Task / TaskStore,会话级任务清单
│   ├── tools/
│   │   ├── registry.py           # ToolRegistry
│   │   └── builtin/
│   │       ├── __init__.py       # 聚合 default_tools()
│   │       ├── files.py          # read_file / write_file / list_dir + workspace 限制
│   │       ├── shell.py          # 跨平台 shell 工具,requires_approval=True
│   │       └── todo.py           # todo_write 工具(更新 TaskStore)
│   └── util/
│       ├── menu.py               # 方向键内联菜单(prompt_toolkit Application)
│       └── redact.py             # 凭据脱敏
├── config/
│   ├── models.yaml               # 模型 profile（local_qwen / cloud_claude / cloud_openai 等）
│   └── app.example.yaml          # 应用配置模板
├── tests/
│   └── unit/
│       ├── test_approval.py
│       ├── test_compatible_adapter.py
│       ├── test_openai_adapter.py
│       ├── test_shell_tool.py
│       ├── test_cli_spinner.py
│       ├── test_workspace_paths.py
│       ├── test_redact.py
│       └── test_runtime.py
├── docs/
│   ├── technical_architecture.md # 系统设计方案（最终目标，不描述进度）
│   └── process.md                # 本文件
└── .env.example
```

---

## 已完成

### P0 核心骨架

- **模型层**：`ModelResponse` / `ToolCall` 内部协议；Anthropic adapter（chat + 工具循环）；OpenAI-compatible adapter（chat + 工具循环，Ollama 可用）；`build_model_router()` 工厂按 provider 选 adapter
- **配置**：唯一入口 `.env` + `config/models.yaml` profile；不再从 `~/.claude/settings.json` 兜底；未指定 ACTIVE_PROFILE 时直接报错
- **Agent Runtime**：多轮 tool_use 循环；`requires_approval` 工具审批（y/a/n）；token 用量与耗时统计
- **工具**：`read_file` / `write_file`（需审批）/ `list_dir`
- **CLI**：`--profile` / `-p` / `-y` / `/reset` 命令；spinner 进度动画
- **测试**：`test_approval.py`（6 个）；`test_compatible_adapter.py`（4 个）

### P0 增强：流式与输入体验

- **流式输出**：两个 adapter 都改为流式调用；新增 `ProgressCallback` / `TextDeltaCallback` 类型；token 实时增长（Anthropic 用真实 `message_delta` 配合字符数估算兜底，OpenAI 用 `stream_options.include_usage` 配合估算）
- **CLI spinner**：`✻ thinking… (Xs · ↓ N tokens)` 钉在屏幕底部，文本流式输出在它上方；按显示宽度（中文 2 / 英文 1）+ 终端列宽算实际占用行数，软换行也能正确清屏；spinner 退出时只保留正文，不留摘要污染
- **prompt_toolkit 输入框**：`_repl()` 用 `PromptSession` 替换内建 `input()`，解决中文宽字符 backspace 残留问题；带蓝色提示符 `▸` 与灰色分隔线区隔；自带历史回放（↑/↓）、Ctrl-A/E/W 编辑；Ctrl-C 清空当前行、Ctrl-D 退出

### P0 收尾

- **workspace 路径限制** (`app/tools/builtin/files.py` + `app/config/loader.py:workspace_root`)：所有文件工具受 `WORKSPACE_ROOT` 限制，越界返回 `refused: path '/etc/passwd' is outside workspace '...'`，模型可见可理解
- **错误脱敏** (`app/util/redact.py`)：`format_exception` / `format_traceback` 用正则脱敏 `Bearer xxx` / `sk-ant-xxx` / `sk-xxx` / `cr_xxx` / `x-api-key=xxx` 等；CLI `_repl` 异常处理与 `main()` 顶层兜底都接入
- **能力声明 + 启动校验** (`app/config/schemas.py:LLMConfig.capabilities`)：profile.capabilities 透传到 LLMConfig；CLI banner 显示 `能力: chat, tools`；缺 `tools` 但注册了工具时给警告
- **Agent 离线测试** (`tests/unit/test_runtime.py`)：FakeRouter mock 覆盖 6 个场景：纯文本答案、单次工具循环、审批拒绝（验证 DENIED_MESSAGE 喂回模型）、工具异常（is_error=True 传递）、max_steps 超限、progress/text_delta 回调

### P1 启程：OpenAI 原生 adapter + Shell 工具

- **tool_result 抽象**：协议加 `ToolResult` dataclass；`ModelRouter.format_tool_results(results)` 由各 adapter 自管格式（Anthropic 一条 user 多个 block / Chat Completions 多条 role=tool / Responses API 多条 function_call_output）；`ModelResponse.provider_payload` 改为 `list[dict]`，Runtime 用 `messages.extend` 追加；为接 OpenAI Responses API 铺路
- **OpenAI 原生 adapter** (`app/models/openai_adapter.py`)：调 OpenAI Responses API（不是 Chat Completions），扁平工具定义、`instructions` 顶级 system、流式监听 `response.output_text.delta`，最终从 `stream.get_final_response().output` 重建 ToolCall 与 raw payload；`router.py` 注册 `openai` provider；mock 测试 5 个覆盖文本流式 / function_call 解析 / provider_payload 结构 / format_tool_results / 进度回调
- **跨平台 Shell 工具** (`app/tools/builtin/shell.py`)：Unix 用 `bash -c`，Windows 用 `powershell -NoProfile -Command`，cwd 锁定 `workspace_root`，30s 默认超时，输出 8KB 截断，stdout/stderr/exit code 三段拼接；`requires_approval=True` 强制审批；`app/tools/builtin/__init__.py` 提供聚合 `default_tools()`；mock subprocess 9 个测试覆盖 cwd / 超时 / 截断 / 平台分支

### P1 体验：审批菜单 + 任务面板

- **方向键审批菜单** (`app/util/menu.py`)：用 prompt_toolkit `Application(full_screen=False, erase_when_done=True)` 实现内联菜单；header 显示工具名 + 参数预览；选项 `允许这次 / 本会话总是允许 / 拒绝`；↑↓ 移动 / Enter 确认 / 数字快捷键 / Esc 取消；非 TTY 退化为 stdin 数字输入。`InteractivePolicy` 改用 `select_menu` 替换 `input("y/a/n")`；6 个新单元测试 + menu fallback 6 个
- **任���面板 + todo_write 工具** (`app/agent/tasks.py` + `app/tools/builtin/todo.py`)：`Task` / `TaskStore` 维护会话级任务清单；`make_todo_write_tool(store)` 工厂绑定 store 给模型用；status 取值 `pending / in_progress / completed`，未知值规范化为 pending；`AgentSession` 持有 `task_store`，`/reset` 时清空
- **CLI 任务面板渲染** (`app/cli.py`)：`_Spinner` 在重绘时把任务列表画在最上方,流式文本居中,spinner 钉底；汇总行 `N tasks (X done, Y in progress, Z open)`；任务行 `✓ done` (dim) / `❯ in_progress` (蓝色加粗) / `○ pending`；`_make_progress(task_store)` 工厂注入；4 个新单元测试覆盖渲染逻辑

**测试规模**：21 → **78 个单元测试**（新增 57 个：workspace 7 + redact 9 + runtime 6 + openai_adapter 5 + shell 9 + compatible 1 + approval 重写 7 + menu 6 + tasks 9 + cli_spinner +4 任务面板）

---

## 接下来要做

### P1：模型与交互体验完整

| 任务 | 完成标准 |
|---|---|
| 流式事件结构化 | 在 `ModelResponse` 之上增加 `text_delta` / `tool_call_start` / `tool_call_delta` 等事件，CLI / Web UI 可订阅 |
| SQLite 会话持久化 | `app/storage/sqlite.py`；`sessions` / `messages` / `runs` / `tool_executions` 表；`/history` 命令恢复历史会话；启动时自动加载最近会话 |
| FastAPI Web UI | `app/server.py` + `app/web/`；SSE 事件流；审批 API；与 CLI 共用同一 AgentSession |

### P2：Skill 与 MCP

| 任务 | 完成标准 |
|---|---|
| Skill Loader | `app/skills/loader.py`；扫描 `skills/` 目录；解析 `SKILL.md` metadata；按需注入提示与工具约束 |
| MCP Manager | `app/mcp/manager.py`；stdio + Streamable HTTP transport；工具发现后注册到 ToolRegistry，统一过审批策略 |
| UI 配置面板 | 可查看 / 启用 Skill 与 MCP Server，显示工具列表与风险等级 |

### P3：知识与发布

| 任务 | 完成标准 |
|---|---|
| RAG / 本地知识库 | 嵌入 + 向量检索，不影响核心对话路径 |
| 系统 Keyring | macOS/Windows 系统 Keyring 存储 API Key，不再依赖明文 `.env` |
| 打包评估 | 根据 Web 版本使用反馈决定是否引入 Tauri 等桌面壳 |

---

## 运行验证

```bash
# 全部单元测试
python -m pytest tests/unit/ -v

# 用 Claude 跑通（需 .env 有 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL）
python -m app --profile cloud_claude -p "list_dir 看下 config/ 目录" -y

# 用本地 Ollama 跑通（需先 ollama pull qwen2.5-coder:7b-instruct && ollama serve）
python -m app --profile local_qwen -p "list_dir 看下当前目录" -y

# 交互模式（带流式 + 输入框）
python -m app
```
