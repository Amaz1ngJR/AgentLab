# AgentLab 进展与交接文档

本文档用于让新的 AI 或开发者快速接手项目：先看当前进展，再按 PRD 继续推进。总体目标和最终设计只维护在 [`technical_architecture.md`](./technical_architecture.md)，不要把阶段进展、更新时间或临时代码状态写进 PRD。

维护规则：

- `technical_architecture.md` 是目标方案和 PRD，只描述“最终应该是什么”。
- `process.md` 是当前进展和执行计划，必须始终写清楚“现在做到哪里、还缺什么、下一步先做什么”。
- 每次完成一个模块后，同步更新本文件的“当前能力快照”“模块进展与下一步”“运行与验证”。
- 当前需要做的事情最多保留 5 项，避免变成无限待办清单。

---

## 1. 当前一句话状态

AgentLab 当前已经是一个可运行的本地 CLI Agent MVP：支持模型 profile 切换、云端/本地模型 adapter、流式输出、多轮工具调用、审批、内置文件/代码搜索/shell/todo 工具，以及 stdio MCP Client 和 Playwright MCP 浏览器控制。

距离 PRD 的核心缺口是：还没有多 Agent 与 `/session` 会话切换、没有长期记忆和 SQLite 持久化、没有显式 `Planner + Executor + Replanner + TaskStore` 编排、没有统一风险等级 ToolDescriptor、没有自建 Computer Control Gateway、没有 Web UI。

---

## 2. 当前需要做的事情

1. 实现 `AgentProfile + SessionRouter + /session`，让一个应用里可以创建、列出、切换多个 Agent 会话。
2. 引入 SQLite 持久化，先落 `agent_profiles / sessions / messages / tasks / memories / tool_executions`，支撑会话恢复和长期记忆。
3. 将现有 `AgentSession` 拆成 `Planner + Executor + Replanner + TaskStore`，并输出结构化 `RunEvent`。
4. 升级工具系统为统一 `ToolDescriptor`，补齐 `risk / target / scope / origin / audit` 等元数据和分级审批。
5. 建立 `ComputerControlGateway`，先把 Playwright MCP 浏览器能力纳入统一观察、动作、审批、审计链路。

---

## 3. 当前能力快照

| 模块 | 当前进展 | 关键文件 |
|---|---|---|
| CLI | 支持交互 REPL、单次 `-p/--prompt`、`--profile`、`-y` 自动审批、`/reset`；有 prompt_toolkit 输入、spinner、任务面板、token/耗时统计 | `app/cli.py` |
| 配置 | 以 `.env` + `config/models.yaml` 为模型配置来源；支持 `ACTIVE_PROFILE` 或 `--profile`；文件工具受 `WORKSPACE_ROOT` 限制 | `app/config/loader.py`, `app/config/schemas.py`, `config/models.yaml` |
| 模型层 | 已有 Anthropic、OpenAI Responses、OpenAI-compatible adapter；内部协议统一为 `ModelResponse / ToolCall / ToolResult`；OpenAI-compatible 具备 JSON tool call fallback | `app/models/` |
| Runtime | 现有同步多轮“模型 -> 工具 -> 模型”循环；支持工具审批、工具错误回灌、流式文本回调、会话级消息历史和统计 | `app/agent/runtime.py` |
| 任务面板 | 已有轻量 `TaskStore` 和 `todo_write`，用于 CLI 展示任务清单；还不是 PRD 里的任务状态源 | `app/agent/tasks.py`, `app/tools/builtin/todo.py` |
| 审批 | 支持自动审批、交互审批、拒绝策略；交互审批使用方向键菜单 | `app/agent/approval.py`, `app/util/menu.py` |
| 内置工具 | 已有 `read_file / write_file / list_dir / code_search / shell / todo_write`；`code_search` 支持 text/regex/file/symbol，优先 `rg --json`，无 rg 时 Python fallback | `app/tools/builtin/` |
| MCP | 已有 stdio MCP Manager、配置加载、工具发现、sync/async 桥、同名工具不覆盖内置、auto_approve 白名单 | `app/mcp/` |
| 浏览器控制 | 通过 Playwright MCP 可打开页面、snapshot、点击、输入；支持 named persistent profile；云端模型启用浏览器 MCP 时有数据边界提示 | `config/mcp_servers.example.yaml`, `app/cli.py` |
| 安全基础 | 已有 workspace 越界拒绝、错误和工具输出脱敏、MCP env allowlist、浏览器 profile 目录不入库 | `app/util/redact.py`, `app/tools/builtin/files.py`, `.gitignore` |
| 测试 | unit 测试收集到 143 个；覆盖 runtime、adapter、MCP、code_search、shell、审批、spinner、workspace path 等 | `tests/unit/` |

---

## 4. 当前代码结构

```text
AgentLab/
  app/
    cli.py                         # CLI 入口、REPL、spinner、MCP 启动、数据边界提示
    config/
      loader.py                    # .env + models.yaml + WORKSPACE_ROOT
      schemas.py                   # LLMConfig / ModelProfile
    models/
      protocol.py                  # ToolCall / ToolResult / ModelResponse
      anthropic_adapter.py         # Anthropic Messages API
      openai_adapter.py            # OpenAI Responses API
      compatible_adapter.py        # Ollama / LM Studio / vLLM 等 OpenAI-compatible
      router.py                    # provider -> adapter
    agent/
      runtime.py                   # 当前 AgentSession 工具循环
      approval.py                  # 审批策略
      tasks.py                     # 当前轻量 TaskStore
    tools/
      registry.py                  # 当前 Tool 注册表，仍是 requires_approval 布尔模型
      builtin/
        files.py                   # read_file / write_file / list_dir
        code_search.py             # 高频代码搜索
        shell.py                   # 跨平台 shell
        todo.py                    # todo_write
    mcp/
      config.py                    # mcp_servers.yaml loader
      manager.py                   # stdio MCP 生命周期和调用桥
      adapter.py                   # MCP tool -> Tool
    util/
      menu.py                      # 方向键菜单
      redact.py                    # 脱敏
  config/
    models.yaml                    # 模型 profile
    mcp_servers.example.yaml       # MCP 模板
    app.example.yaml               # 应用配置模板
  docs/
    technical_architecture.md      # PRD 和总体技术方案
    process.md                     # 当前进展和接下来工作
  tests/unit/                      # 当前主要测试集
```

尚未出现但 PRD 已规划的目录：`app/memory/`、`app/storage/`、`app/control/`、`app/skills/`、`app/server.py`、`app/web/`。

---

## 5. 模块进展与下一步

### 5.1 Runtime 与任务拆解

当前进展：

- `AgentSession` 可以完成多轮工具调用、审批、工具结果回灌、流式输出和 max_steps 限制。
- `TaskStore` 目前主要服务 `todo_write` 和 CLI 任务面板，任务结构较轻。

接下来要做：

- 新增 `app/agent/planner.py`，把复杂用户目标拆成 `TaskPlan`。
- 新增 `app/agent/executor.py`，按依赖从 TaskStore claim 下一步任务并执行。
- 新增 `app/agent/replanner.py`，根据工具结果、错误、审批拒绝和用户追加目标调整任务。
- 升级 `tasks.py`，支持 dependencies、blocked、failed、evidence、history、snapshot。
- 定义结构化 `RunEvent`，至少包括 `message_delta / tool_requested / approval_required / tool_completed / task_updated / run_completed / run_failed`。

第一验收标准：

- fake model 测试覆盖：初始计划、按依赖执行、工具失败后重规划、审批拒绝后阻塞、用户追加目标、取消和 max_steps。

### 5.2 多 Agent、Session 与长期记忆

当前进展：

- 已实现 `AgentProfile`（`app/agent/profiles.py`）：定义 agent_id、name、model_profile、system_prompt、tools、mcp_servers、memory_policy、max_steps；`load_agent_profiles()` 读 `config/agents.yaml`（不存在时返回空，向后兼容）。
- 已实现 `SQLite 存储层`（`app/storage/__init__.py`）：建表 agent_profiles / sessions / messages / memories / tool_executions；`Storage` 类提供会话 CRUD、消息存盘/加载、记忆写入/LIKE 搜索、工具执行审计；所有写入经 `redact()` 脱敏。
- 已实现 `SessionRouter`（`app/agent/session_router.py`）：维护 session_id → AgentSession 映射；支持 `/session`、`/session list`、`/session agents`、`/session new`、`/session switch`、`/session rename`、`/session archive`；消息历史可从 SQLite 恢复；两个 Session 的 messages 完全隔离。
- 已实现长期记忆层（`app/memory/__init__.py`）：`NoMemory / ReadMemory / ReadWriteMemory` 三种策略；`build_memory_policy()` 工厂；`inject_memories()` 把检索结果追加到 system prompt；`read_write` 策略在会话结束时自动把对话摘要写入记忆。
- 已有 `config/agents.example.yaml` 模板（default / coder / local 三个示例 profile）。
- 新增 31 个单元测试，全量 **174 passed**。

接下来要做：

- CLI 接入 `SessionRouter`：启动时初始化 Storage + Router，REPL 里把 `/session ...` 命令转给 Router 处理，`chat()` 后自动 `persist_current()`，会话结束时按 memory_policy 写摘要。
- Context Builder：`AgentSession` 构建时按 AgentProfile.memory_policy 检索记忆并调用 `inject_memories()` 注入 system prompt。
- CLI banner 显示当前 session_id 和 agent 名称，prompt 提示符带 session 标识。

第一验收标准（已满足核心部分）：

- `SessionRouter` 单元测试验证：两个 session 消息互不串（`test_two_sessions_are_isolated`）；切换后从 SQLite 恢复消息历史（`test_switch_restores_from_sqlite`）。
- CLI 接入后的端到端验证：创建两个 Agent，切换，消息不串，重启后历史可恢复。

### 5.3 模型层

当前进展：

- 支持 Anthropic、OpenAI Responses API、OpenAI-compatible。
- 本地模型可通过 Ollama/兼容接口使用；普通模型如果不支持 tools，能力仍不完整。
- 已有实际模型 ID 规范化和代理静默映射提示。

接下来要做：

- 启动时统一校验 `chat / tools / streaming / json_action` 能力。
- 为不支持原生 tools 但能稳定输出 JSON 的模型补 `json_action_adapter`，并默认限制高风险工具。
- 建立 Ollama、LM Studio、vLLM 的兼容矩阵，记录工具调用、流式、上下文长度限制。
- 把云端数据边界提示从 CLI banner 升级为 RunEvent 级别，让 Web UI 也能显示。

第一验收标准：

- 同一个 AgentProfile 能在支持 tools 的云端模型和本地模型间切换；不支持 tools 的模型不会被误放行执行危险工具。

### 5.4 工具与审批

当前进展：

- `Tool` 只有 `requires_approval` 布尔字段。
- 内置工具已经覆盖基础文件、搜索代码、shell 和 todo。
- `code_search` 是高频只读工具，应优先于 shell 拼 `grep/find`。

接下来要做：

- 将 `Tool` 升级为 PRD 中的 `ToolDescriptor`，补 `risk / target_type / scope / origin / host / requires_observation / audit_redactor`。
- 审批从布尔值升级为分级策略：`read / observe / network / write / browser_control / desktop_control / remote_execute / execute / destructive`。
- 支持会话级授权，但授权必须绑定 tool、origin、host、workspace；删除、支付、发布、上传等动作不能被普通授权绕过。
- 内置工具、MCP 工具、浏览器动作、远程动作统一进入审计摘要。

第一验收标准：

- 同一个审批策略可以同时判断 `write_file`、`shell`、`browser_click`、MCP tool 和 remote command。

### 5.5 MCP

当前进展：

- stdio MCP Client 已可用。
- Playwright MCP 已接入，可作为当前浏览器控制能力。
- MCP 工具默认需要审批，只有 auto_approve 白名单中的只读工具免审批。
- MCP 同名工具不会覆盖内置基础工具。

接下来要做：

- 增加 Streamable HTTP transport。
- MCP 工具映射到新版 `ToolDescriptor`，继承 server risk，并可按工具提高风险等级。
- MCP 调用写入 `tool_executions` 审计表。
- 增加健康状态、断线重连、连接失败事件。

第一验收标准：

- stdio 和 Streamable HTTP 两种 MCP 都能通过统一 ToolRegistry 调用，并能被同一套审批和审计策略处理。

### 5.6 Computer Control Gateway

当前进展：

- 还没有 `app/control/`。
- 浏览器控制目前直接通过 Playwright MCP 暴露为工具，没有经过统一 ControlGateway。
- 桌面控制和远程设备控制未实现。

接下来要做：

- 新增 `app/control/sessions.py`，定义 `ControlTarget / ControlSession / Observation / ControlAction`。
- 新增 `app/control/gateway.py`，所有 browser、desktop、remote 动作必须经过目标校验、风险判断、审批、执行、审计。
- 先把 Playwright MCP 包装成 browser backend，再考虑自建 Playwright Python adapter。
- 新增 `config/control.example.yaml`，声明 browser、desktop_control、remote_hosts。

第一验收标准：

- browser snapshot/click/type 都通过 ControlGateway 产生 observation、approval request 和 audit record。

### 5.7 浏览器控制

当前进展：

- Playwright MCP 可用，适合打开网页、观察无障碍树、点击、输入。
- named persistent profile 已有配置说明，可以保留登录态。
- 云端模型使用浏览器 MCP 时 CLI 会提示页面数据会进入云端模型上下文。

接下来要做：

- 细化按 origin 的审批，登录、支付、授权、删除、上传、发布动作二次确认。
- 截图和 snapshot 统一落到受控 data 目录，并以 observation id 引用。
- 增加本地测试页面，覆盖点击、输入、导航、下载路径限制。
- 对富文本系统和内部文档修改场景，优先设计 REST API/MCP 工具，不依赖脆弱 DOM 点击完成精确编辑。

第一验收标准：

- Agent 能在本地测试页完成打开、观察、点击、输入、提交前确认，并能在审计里回看关键动作。

### 5.8 远程设备控制

当前进展：

- 未实现。

接下来要做：

- 新增远程 host 配置，只允许预配置 host。
- SSH Runner 必须做 host key 校验、workspace 限制、timeout、输出截断、stderr/exit code 结构化。
- 文件传输只允许本地 workspace 与 remote workspace 之间传输。
- 复杂远程 GUI 操作优先走远程 Agent Worker 或 Remote Host MCP。

第一验收标准：

- Agent 可以在配置过的远程 host 的指定 workspace 内执行只读命令；越界、未知 host、危险命令都被拒绝或要求强审批。

### 5.9 Skill

当前进展：

- 未实现 Skill Loader。
- 当前 system prompt 中有硬编码行为准则，但还不能按任务动态加载 Skill。

接下来要做：

- 新增 `app/skills/loader.py` 和 `app/skills/catalog.py`，扫描 `skills/*/SKILL.md`。
- 校验 Skill metadata，支持启用/禁用、触发规则、参考资料、工具/MCP 需求声明。
- Skill 只能影响上下文和可见工作流，不能自动获得工具授权。

第一验收标准：

- 一个 Coding Skill 可以按 AgentProfile 启用，并把工作流说明注入上下文；未授权工具仍不能被调用。

### 5.10 存储、Web UI 与发布

当前进展：

- 没有 SQLite 持久化。
- 没有 FastAPI Web UI。
- 会话、消息、任务、审计都只在当前进程内存中。

接下来要做：

- 新增 `app/storage/sqlite.py`，先实现 `agent_profiles / sessions / messages / runs / tasks / memories / tool_executions / settings`。
- CLI 与未来 Web UI 共用同一个 Runtime service，而不是各自创建不同逻辑。
- 新增 `app/server.py` 和 `app/web/`，提供本地 Web UI、SSE 事件、审批 API、Stop 按钮。
- 配置面板能查看 AgentProfile、模型 profile、Skill、MCP Server、Control Target 和工具风险等级。

第一验收标准：

- 退出重启后可以 `/session list` 看到历史 session，并恢复消息、任务和记忆摘要。

### 5.11 安全、可观测性与测试

当前进展：

- 已有 workspace 限制、脱敏、MCP 环境变量 allowlist、审批基础能力。
- 测试目前主要是 unit，集成测试目录存在但还未形成主路径。

接下来要做：

- API Key 从 `.env` 逐步迁移到 macOS/Windows Keyring，`.env` 只做开发兜底。
- 所有 tool execution、approval、control action、model profile、actual model 都要进入审计事件。
- 增加 provider fake、MCP test server、本地浏览器测试页、fake SSH target。
- 高风险模块默认禁用，首次启用必须展示能力、数据边界和风险。

第一验收标准：

- 一次包含模型推理、工具调用、审批、浏览器 observation 的 run 可以被完整回放为事件和审计记录。

---

## 6. MCP 接入路线

原则：内置 `read_file / write_file / list_dir / code_search / shell` 是基础能力，不被 MCP 替代。MCP 主要用于连接外部系统、专业工具和可选增强能力。若 MCP 提供同类能力，应作为增强 backend 或用户显式选择的工具。

| 优先级 | MCP 类型 | 当前状态 | 下一步 |
|---|---|---|---|
| P0 | Playwright MCP | 已接入 stdio，作为当前浏览器控制能力 | 纳入 ControlGateway，补 origin 级审批和审计 |
| P1 | Git MCP | 未接入 | 先做只读 status/diff/log/branch；checkout/commit/reset 必须审批 |
| P1 | GitHub MCP | 未接入 | issue/PR 读取先行；评论、改 PR、触发 workflow 前审批；token 不入日志 |
| P1 | IDE/LSP MCP | 未接入 | diagnostics、definition、references、symbol search，用于增强 `code_search` |
| P2 | Database MCP | 未接入 | 默认只读 schema/query；写 SQL/DDL 默认禁用或强审批 |
| P2 | Remote Host MCP | 未接入 | 与 SSH Runner 互补，只允许预配置 host 和 remote workspace |
| P2 | Docs/Search MCP | 未接入 | 内部文档、API 文档、知识库检索；结果必须标注来源 |
| P3 | Cloud/DevOps MCP | 未接入 | 只读观察先接入；部署、扩缩容、删除资源必须二次确认 |

---

## 7. 运行与验证

推荐环境是 conda 环境 `agentlab`，Python 3.11。当前系统 `python` 命令可能不存在，直接用系统 `python3` 也可能没有项目依赖；优先使用下面的命令。

```bash
conda activate agentlab

# 收集测试。当前收集到 143 个 unit tests。
python -m pytest tests/unit --collect-only -q

# 运行全部 unit tests。
python -m pytest tests/unit -q

# 交互模式。
python -m app

# 单次 prompt。
python -m app --profile cloud_claude -p "list_dir 看下 config/ 目录"

# 本地模型。需要先启动 Ollama 并下载模型。
python -m app --profile local_qwen -p "list_dir 看下当前目录"
```

Playwright MCP 浏览器控制验证：

```bash
cp config/mcp_servers.example.yaml config/mcp_servers.yaml
# 编辑 config/mcp_servers.yaml，把 playwright.enabled 改成 true。

python -m app --profile cloud_claude -p "打开 https://example.com 并告诉我页面标题"
```

注意：

- Playwright MCP 首次启动可能通过 `npx` 下载 server 和浏览器内核。
- 使用 named persistent profile 时，登录态会保存在 `data/browser-profiles/<name>`，该目录不能入库。
- 云端模型配合浏览器控制时，页面 DOM、截图摘要、表单内容可能进入云端模型上下文。

---

## 8. 接手注意事项

- 先读 PRD 的目标设计，再读本文件判断当前代码缺口。
- 做实现时优先保持现有模式：Python dataclass、pytest unit test、fake provider/fake manager、workspace 限制、脱敏。
- 查代码优先用 `rg` 或 Agent 内置 `code_search`，不要让模型通过 shell 拼复杂 grep/find。
- 修改 PRD 时只改目标设计；修改当前进度、阶段完成情况、下一步计划时只改本文件。
- 高风险能力的顺序应是：先结构化描述和审计，再接真实执行能力。
