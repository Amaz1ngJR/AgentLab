# AgentLab 开发进度

本文档供 AI 协作时参考：当前已实现什么、PRD 还缺什么、每个模块接下来要做什么。

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
│       ├── test_menu.py
│       ├── test_compatible_adapter.py
│       ├── test_openai_adapter.py
│       ├── test_shell_tool.py
│       ├── test_tasks.py
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
- **任务面板 + todo_write 工具** (`app/agent/tasks.py` + `app/tools/builtin/todo.py`)：`Task` / `TaskStore` 维护会话级任务清单；`make_todo_write_tool(store)` 工厂绑定 store 给模型用；status 取值 `pending / in_progress / completed`，未知值规范化为 pending；`AgentSession` 持有 `task_store`，`/reset` 时清空
- **CLI 任务面板渲染** (`app/cli.py`)：`_Spinner` 在重绘时把任务列表画在最上方,流式文本居中,spinner 钉底；汇总行 `N tasks (X done, Y in progress, Z open)`；任务行 `✓ done` (dim) / `❯ in_progress` (蓝色加粗) / `○ pending`；`_make_progress(task_store)` 工厂注入；4 个新单元测试覆盖渲染逻辑

**测试规模**：21 → **78 个单元测试**（新增 57 个：workspace 7 + redact 9 + runtime 6 + openai_adapter 5 + shell 9 + compatible 1 + approval 重写 7 + menu 6 + tasks 9 + cli_spinner +4 任务面板）

---

## 当前 PRD 差距

`technical_architecture.md` 已定位为最终目标/PRD，不再记录当前实现状态。按该目标对照，当前代码主要缺口如下：

| 模块 | 当前状态 | 与 PRD 的差距 |
|---|---|---|
| Agent Runtime | 已有同步多轮工具循环、审批、流式文本桥接；已有会话级 TaskStore/todo 面板 | 缺显式 Planner + Executor + Replanner 任务拆解模块；事件模型还不够结构化，Web/SSE、取消、持久化 run 状态未完成 |
| 模型层 | Anthropic、OpenAI-compatible、OpenAI adapter 已有骨架和测试 | 需要统一 profile 能力探测；普通 chat/instruct 模型的 JSON action parser 尚未设计实现 |
| 工具层 | 文件、shell、todo 已有；风险只靠 `requires_approval` 布尔值 | 缺高频 `code_search`；需要升级为风险等级、target、scope、origin/host、审计元数据 |
| Computer Control | 未实现 | 缺 `app/control/`，包括 Browser Adapter、Desktop Adapter、Remote Runner、Control Session |
| 浏览器控制 | 未实现 | 缺 Playwright 集成、受控浏览器 profile、截图/DOM snapshot、点击/输入/下载工具 |
| 远程设备控制 | 未实现 | 缺 `config/control.yaml`、SSH host 校验、远程 workspace、远程命令/文件传输工具 |
| 桌面控制 | 未实现 | 缺权限检测、截图、坐标动作、紧急停止；该能力应默认禁用 |
| Skill | 未实现 | 缺 `SKILL.md` loader、metadata 校验、按需注入和工具约束 |
| MCP | 未实现 | 缺 MCP Manager、stdio/Streamable HTTP client、MCP 工具注册和统一审批 |
| 存储 | 未实现 | 缺 SQLite schema、会话恢复、tool_executions 审计 |
| Web UI | 未实现 | 缺 FastAPI server、SSE 事件、审批 API、控制目标观察界面 |
| 安全 | 已有 workspace 限制与错误脱敏 | 缺密钥 Keyring、分级审批、浏览器/远程/桌面专属安全策略落地 |

---

## 接下来要做（按模块）

### 1. Runtime 与事件模型

| 任务 | 完成标准 |
|---|---|
| Planner | 新增 `app/agent/planner.py`；复杂任务生成 TaskPlan，包含任务 id、title、description、dependencies、risk_hint、expected_evidence |
| Executor | 新增 `app/agent/executor.py`；从 TaskStore claim 下一个可执行任务，驱动模型和工具执行，并写入 evidence/status |
| Replanner | 新增 `app/agent/replanner.py`；根据工具结果、错误、用户拒绝、环境变化追加/修改/阻塞任务 |
| TaskStore 升级 | 将当前 TaskStore 从展示型 todo 升级为任务状态源，支持 dependencies、blocked、failed、evidence、history、snapshot |
| 结构化 RunEvent | 定义 `message_delta` / `tool_requested` / `approval_required` / `tool_completed` / `control_observation` / `run_completed` / `run_failed` 等事件；CLI 和未来 Web UI 只消费事件 |
| 任务事件 | 新增 `task_plan_created` / `task_started` / `task_updated` / `task_blocked` / `task_completed` / `task_replanned`，CLI/Web 都通过事件渲染任务面板 |
| 可取消 run | CLI Ctrl-C 或 Web Stop 能取消模型请求、工具执行和控制动作；取消记录进入审计 |
| 会话状态边界 | 将 Runtime 状态拆成 session、run、messages、tool_results，方便 SQLite 和 Web API 复用 |
| 普通模型动作解析 | 为不支持 tools 但 JSON 稳定的模型设计 `json_action_adapter`，通过测试后才允许启用 Agent 工具 |
| Planner/Executor/Replanner 测试 | fake model 覆盖初始计划、按依赖执行、工具失败后重规划、审批拒绝后阻塞、用户追加目标、取消和 max_steps |

### 2. 模型层

| 任务 | 完成标准 |
|---|---|
| profile 能力探测 | 启动时校验 chat/tools/streaming 能力；工具不可用时阻止高风险 Agent 会话 |
| OpenAI-compatible 兼容矩阵 | Ollama、LM Studio、vLLM 至少各有连通测试或文档化限制 |
| 云端数据边界提示 | 当 profile 是云端模型且工具结果含文件、截图、DOM、远程输出时，事件层能标记数据将离开本机 |

### 3. 工具与审批

| 任务 | 完成标准 |
|---|---|
| `code_search` 内置工具 | 新增 `app/tools/builtin/code_search.py`；支持 text/regex/file/symbol 四种模式；优先 `ripgrep`，无 `rg` 时 Python fallback；结果返回相对路径、行号、列号、preview、context、truncated |
| ToolDescriptor 扩展 | `Tool` 增加 `risk`、`target_type`、`scope`、`requires_observation`、`audit_redactor` 等字段 |
| 分级审批策略 | 从布尔审批升级为 read/observe/network/write/browser_control/desktop_control/remote_execute/execute/destructive |
| 会话级授权 | 支持按 tool、origin、host、workspace 授权；支付、删除、发布、上传等动作不允许被普通会话授权覆盖 |
| 审计摘要 | 工具参数和输出进入审计前脱敏和截断，保留可追踪但不泄露密钥 |
| `code_search` 测试 | 覆盖 text/regex/file/symbol、workspace 越界、`.gitignore`、默认忽略目录、rg fallback、max_results、context_lines、timeout、二进制跳过和疑似密钥脱敏 |

### 4. Computer Control Gateway

| 任务 | 完成标准 |
|---|---|
| `app/control/sessions.py` | 定义 ControlTarget、ControlSession、Observation、ControlAction；管理生命周期和截图引用 |
| `ControlGateway` | 所有浏览器/桌面/远程动作必须经过 target 校验、风险判断、审批、执行、审计 |
| 配置文件 | 新增 `config/control.example.yaml`，覆盖 browser、desktop_control、remote_hosts |
| 测试 | fake target 覆盖允许、拒绝、取消、未知 target、越权 capability、审计记录 |

### 5. 浏览器控制

| 任务 | 完成标准 |
|---|---|
| Playwright adapter | 支持启动隔离 Chromium profile、打开 URL、关闭会话 |
| Snapshot | 返回 URL、title、可交互元素摘要、可选截图；截图文件写入受控数据目录 |
| 网页动作工具 | `browser_open`、`browser_snapshot`、`browser_click`、`browser_type`、`browser_press`、`browser_wait` |
| 安全策略 | 按 origin 审批；登录、支付、授权、删除、上传、发布动作二次确认 |
| 本地测试页 | 用本地静态 HTML 覆盖点击、输入、导航、下载路径限制 |

### 6. 远程设备控制

| 任务 | 完成标准 |
|---|---|
| 远程 host 配置 | 只允许配置文件中的 host；凭据只引用环境变量或 Keyring，不写明文 |
| SSH Runner | host key 校验、workspace 限制、timeout、输出截断、stderr/exit code 结构化 |
| 文件传输 | 只允许本地 workspace 与 remote workspace 之间传输；越界额外拒绝或审批 |
| 远程 Worker 方案 | 设计远程 Agent Worker/MCP Server，用于远程浏览器和复杂 GUI 操作 |

### 7. Skill 与 MCP

| 任务 | 完成标准 |
|---|---|
| Skill Loader | 扫描 `skills/`，解析 `SKILL.md` metadata，支持启用/禁用和按需注入 |
| Skill 权限声明 | Skill 可声明需要的工具/MCP，但不能自动获得授权 |
| MCP Manager | 支持 stdio 和 Streamable HTTP；发现工具后映射为 ToolDescriptor |
| MCP 安全 | 新 MCP Server 默认禁用；启用前展示 transport、工具、风险、可能暴露的数据 |

#### 待接入 MCP 清单

原则：内置 `read_file` / `write_file` / `list_dir` / `code_search` / `shell` 是基础能力，不被 MCP 替代。MCP 主要用于连接外部系统、专业工具和可选增强能力；如果 MCP 提供同类能力，应作为增强 backend 或用户显式选择的工具。

| 优先级 | MCP 类型 | 用途 | 与内置能力关系 | 完成标准 |
|---|---|---|---|---|
| P0 | MCP Echo/Test Server | 验证 MCP Manager、stdio transport、工具发现、调用、错误处理 | 纯测试，不面向用户能力 | 本地测试 server 可被发现；工具映射为 ToolDescriptor；审批/审计链路可跑通 |
| P0 | Filesystem MCP（受限 workspace） | 验证外部 MCP 文件工具的安全边界 | 不替代内置文件工具，只用于测试 MCP 工具隔离和同名工具策略 | 同名工具不覆盖内置工具；越界路径被拒绝；工具风险继承 server risk |
| P1 | Browser MCP / Playwright MCP | 网页打开、截图、DOM snapshot、点击、输入 | 可作为 `control/browser.py` 的外部 backend；内置 Browser Adapter 仍保留 | 能打开本地测试页、返回 snapshot、点击和输入；origin 审批生效 |
| P1 | Git MCP | 查看 diff、log、branch、status，辅助代码修改和审查 | 可增强 shell/git 操作，减少模型直接拼命令 | 只读 Git 操作默认 read；checkout/commit/reset 等写操作必须审批 |
| P1 | GitHub MCP | issue、PR、review comment、workflow 状态 | 外部 SaaS 能力，必须经 token 和权限配置 | 能读取 issue/PR；写评论、改 PR、触发 workflow 前审批；token 不入日志 |
| P1 | IDE / LSP MCP | diagnostics、go to definition、references、symbol search | 增强 `code_search` 的 symbol 模式，不替代基础 text/file 搜索 | 能返回 diagnostics 和 symbol references；结果路径受 workspace 限制 |
| P2 | Database MCP（SQLite/Postgres/MySQL） | 查询开发/测试数据库 schema 和只读数据 | 外部数据源能力，风险高于代码搜索 | 默认只读；写 SQL/DDL 默认禁用或二次确认；连接串走环境变量/Keyring |
| P2 | Remote Host MCP | 远程设备上的文件、shell、浏览器能力 | 和 `remote.py` 互补；复杂远程 GUI 优先走远程 Worker/MCP | 只允许预配置 host；host key/身份校验；远程 workspace 限制和审计 |
| P2 | Docs/Search MCP | 内部文档、API 文档、知识库检索 | 补充 RAG，不影响本地代码搜索 | 查询结果标注来源；远程查询按 network 风险审批或会话授权 |
| P3 | Cloud/DevOps MCP | Docker、Kubernetes、CI/CD、云资源 | 高风险外部操作 | 只读观察先接入；部署、扩缩容、删除资源必须二次确认 |

接入顺序建议：

1. 先完成 MCP Manager 的协议框架和 Echo/Test Server。
2. 用受限 Filesystem MCP 验证安全边界和同名工具策略。
3. 接 Browser MCP 或 Playwright MCP，和 Computer Control Gateway 对齐。
4. 接 Git / GitHub / IDE-LSP MCP，服务代码理解和开发工作流。
5. 最后接 Database、Remote Host、Cloud/DevOps 这类高风险 MCP。

### 8. 存储、Web UI 与发布

| 任务 | 完成标准 |
|---|---|
| SQLite 会话持久化 | `app/storage/sqlite.py`；`sessions` / `messages` / `runs` / `tool_executions` 表；`/history` 命令恢复历史会话；启动时自动加载最近会话 |
| FastAPI Web UI | `app/server.py` + `app/web/`；SSE 事件流；审批 API；与 CLI 共用同一 AgentSession |
| 控制观察 UI | Web UI 能展示浏览器截图、DOM 摘要、远程 stdout、待审批动作和 Stop 按钮 |
| UI 配置面板 | 可查看/启用模型 profile、Skill、MCP Server、Control Target，显示工具列表与风险等级 |
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
