# AgentLab 进展与交接文档

本文档用于让新的 AI 或开发者快速接手项目：先看当前进展，再按 PRD 继续推进。总体目标和最终设计只维护在 [`technical_architecture.md`](./technical_architecture.md)，不要把阶段进展、更新时间或临时代码状态写进 PRD。

维护规则：

- `technical_architecture.md` 是目标方案和 PRD，只描述“最终应该是什么”。
- `process.md` 是当前进展和执行计划，必须始终写清楚“现在做到哪里、还缺什么、下一步先做什么”。
- **完成的工作统一归到第 3 节「已完成里程碑」**，不要在各模块的"下一步"表里用删除线或"已完成"标记堆积；模块章节只保留真正待做的事。
- 「当前需要做的事情」最多保留 5 项，避免变成无限待办清单。

---

## 1. 当前一句话状态

AgentLab 是一个可运行的本地 CLI Agent：支持模型 profile 切换、云端/本地 adapter、流式输出、多轮工具调用、方向键审批、内置文件/代码搜索/shell/交互式终端/todo 工具、stdio MCP Client（Playwright 浏览器控制）、多 Agent `/session` 切换、SQLite 持久化与长期记忆、Skill Loader（按 AgentProfile 注入工作流上下文）。

距离 PRD 的核心缺口：还没有显式 `Planner + Executor + Replanner` 编排与结构化 `RunEvent`、没有统一风险等级 `ToolDescriptor` 与分级审批、没有自建 Computer Control Gateway、没有 Web UI。

---

## 2. 当前需要做的事情（最多 5 项）

1. 将 `AgentSession` 拆成 `Planner + Executor + Replanner`，升级 `TaskStore` 为任务状态源，并输出结构化 `RunEvent`（见 5.1）。
2. 把 `Tool` 升级为统一 `ToolDescriptor`，审批从布尔值改为分级策略（read/write/execute/browser_control/...）（见 5.4）。
3. 建立 `ComputerControlGateway`，把 Playwright MCP 浏览器能力纳入统一观察、动作、审批、审计链路（见 5.6）。
4. FastAPI Web UI + SSE 事件，与 CLI 共用同一 Runtime service（见 5.10）。

---

## 3. 已完成里程碑

> 完成的能力统一记在这里。各模块章节（第 5 节）只描述"当前状态 + 还要做什么"，不再重复罗列已完成项。

### 3.1 P0 核心 CLI Agent

- CLI REPL：交互模式、单次 `-p/--prompt`、`--profile`、`-y` 自动审批；prompt_toolkit 输入框、spinner 进度（`✻ thinking… (3.2s · ↓ 42 tokens)`）、token/耗时统计。
- 模型层：Anthropic Messages、OpenAI Responses、OpenAI-compatible 三种 adapter；内部协议统一 `ModelResponse / ToolCall / ToolResult`；OpenAI-compatible 具备 JSON tool call fallback；实际模型 ID 规范化 + 代理静默映射提示。
- Runtime：同步多轮"模型 → 工具 → 模型"循环；工具审批、工具错误回灌、流式文本回调、`max_steps` 限制。
- 审批：`AutoApprove / InteractivePolicy(方向键菜单) / DenyAll`。
- 安全基础：workspace 越界拒绝、错误/工具输出脱敏（`redact`）、MCP env allowlist。

### 3.2 内置工具

- `read_file / write_file / list_dir`（受 `WORKSPACE_ROOT` 限制）、`shell`（跨平台、cwd 锁定 workspace）、`todo_write`（CLI 任务面板 `✓/❯/○`）。
- `code_search`（`app/tools/builtin/code_search.py`）：text/regex/file/symbol 四种模式，优先 `rg --json`，无 rg 时 Python fallback；遵守 `.gitignore`、命中行密钥脱敏。
- 交互式终端会话（`app/tools/builtin/interactive.py`）：`PtySession` 在伪终端里起子进程，`read-until-idle` 通用驱动（不依赖提示符/哨兵）；`terminal_open / terminal_send / terminal_close / terminal_list` 四个工具按会话注入；用于远程登录（`zsh -ic 'vsm <device>'`、ssh）、REPL、交互式安装器等 `shell` 搞不定的有状态会话。子进程随 AgentSession 关闭而清理。

### 3.3 MCP Client 层（stdio）+ Playwright 浏览器控制

- `app/mcp/`：`config.py`（读 `mcp_servers.yaml`）、`manager.py`（stdio 生命周期 + sync↔async 调用桥）、`adapter.py`（MCP tool → 内置 Tool）。
- 工具发现后映射为 Tool；同名工具不覆盖内置；`auto_approve` 白名单中的只读工具免审批，其余需审批。
- Playwright MCP 已接入：打开页面、snapshot（无障碍树）、点击、输入。
- named persistent profile：可保留登录态（`--user-data-dir`，profile 目录不入库）。
- 云端数据边界提示：云端模型 + 浏览器 MCP 时，CLI 启动提示页面 DOM/截图/表单会进入云端模型上下文。

### 3.4 多 Agent、Session 切换与长期记忆

- `app/agent/profiles.py`：`AgentProfile`（agent_id/name/model_profile/system_prompt/tools/mcp_servers/memory_policy/max_steps）+ `load_agent_profiles()` 读 `config/agents.yaml`。
- `app/storage/__init__.py`：SQLite（标准库，无 ORM），表 `agent_profiles / sessions / messages / memories / tool_executions`；会话 CRUD、消息存盘/加载、记忆写入/LIKE 搜索、工具执行审计；写入经 `redact()`。
- `app/agent/session_router.py`：`SessionRouter` 维护 session_id → AgentSession 映射；`/session` 命令族 `list / agents / new / switch / rename / archive / delete`；session 间消息完全隔离；可从 SQLite 恢复历史。
  - `archive`=软删除（置 `archived=1`，列表隐藏、可恢复）；`delete`=硬删除（连消息/审计抹除，不可恢复；memories 不随之删除）。
  - 启动 `resume_or_new()`：有未归档历史 session 就恢复最近一个，否则新建（不再每次堆空会话）。
- `app/memory/__init__.py`：`NoMemory / ReadMemory / ReadWriteMemory` 三策略 + `build_memory_policy()` 工厂 + `inject_memories()`（检索结果注入 system prompt）。
- CLI 接入：`_build_session` 构建 Storage + SessionRouter；MCP manager 全局共用（挂 router 上、`close_all` 统一关）；`_session_factory` 按 AgentProfile 装配隔离工具表 + 任务清单 + Context Builder 注入记忆；`_repl` 把 `/session ...` 转给 router，每轮 `chat()` 后 `persist_current()` 存盘。
- REPL 斜杠命令补全（`_SlashCompleter`）：输入 `/` 即弹命令，`complete_while_typing` 边打边弹；三级补全（顶层命令 → `/session` 子命令 → switch/delete 的 session_id、new 的 agent_id）。

### 3.5 配置与模板

- `config/models.yaml`（模型 profile 注册表）+ `config/agents.example.yaml`（多 Agent 模板）+ `config/mcp_servers.example.yaml`（MCP 模板）。
- 约定：`*.example.yaml` 入库作模板，去掉 `.example` 的真实配置 gitignore 不入库；`data/agentlab.db`、`data/browser-profiles/`、`.playwright-mcp/` 均不入库。

### 3.6 Skill Loader

- `app/skills/loader.py`：扫描 `skills/*/SKILL.md`，解析 YAML frontmatter + 工作流正文；校验 metadata（缺 name/description 视为无效，静默跳过）；`skill_id` 取目录名（稳定，不随 frontmatter 漂移）；附带 `references/` 文件路径。
- `app/skills/catalog.py`：`SkillCatalog` 管启用状态（默认取 frontmatter `enabled`，可 `enable/disable`）、`resolve()` 按 `AgentProfile.skills` + 本轮 query 选 Skill、`build_skill_context()/inject()` 把工作流拼进 system prompt。
- 安全边界：Skill 只影响上下文，`allowed_tools` 是"需求/上限"非授权；注入文本显式声明"不授予工具权限"；未知来源 Skill（无 `enabled: true`）默认禁用，需被 `AgentProfile.skills` 显式引用或显式启用才注入。
- `AgentProfile` 新增 `skills` 字段；CLI 启动扫描 catalog 并打印发现/启用数，`_session_factory` 在记忆注入之前注入 Skill 工作流（不放宽工具集）。
- 模板：`skills/code-review/`（SKILL.md + references/checklist.md）；`config/agents.example.yaml` 的 coder 示范 `skills: [code-review]`。

### 3.7 测试

- **235 个 unit tests**（全离线），覆盖：runtime、三种 adapter、MCP（config/adapter/manager）、code_search、shell、交互式终端会话、审批、menu、spinner、workspace path、redact、AgentProfile、storage、memory、session_router、CLI 斜杠补全、Skill loader/catalog。

---

## 4. 当前能力快照

| 模块 | 当前进展 | 关键文件 |
|---|---|---|
| CLI | 交互 REPL、`-p`、`--profile`、`-y`、`/reset`、`/session` 命令族 + 斜杠补全；spinner、任务面板、统计 | `app/cli.py` |
| 配置 | `.env` + `config/models.yaml`（模型）+ `config/agents.yaml`（Agent）+ `config/mcp_servers.yaml`（MCP） | `app/config/`, `app/agent/profiles.py`, `app/mcp/config.py` |
| 模型层 | Anthropic / OpenAI Responses / OpenAI-compatible；统一内部协议；JSON tool call fallback | `app/models/` |
| Runtime | 同步多轮工具循环、审批、错误回灌、流式、max_steps；尚无 Planner/Executor/RunEvent | `app/agent/runtime.py` |
| 多 Agent / Session | AgentProfile + SessionRouter + `/session` 命令族；SQLite 持久化、恢复、隔离 | `app/agent/profiles.py`, `app/agent/session_router.py`, `app/storage/` |
| 长期记忆 | none/read/read_write 三策略 + 注入；LIKE 检索（未做向量） | `app/memory/` |
| Skill | Loader + Catalog：扫 `skills/*/SKILL.md`、按 AgentProfile 注入工作流；只影响上下文不授权 | `app/skills/`, `skills/` |
| 任务面板 | 轻量 `TaskStore` + `todo_write`；还不是 PRD 的任务状态源 | `app/agent/tasks.py`, `app/tools/builtin/todo.py` |
| 审批 | 自动 / 交互（方向键）/ 拒绝；仍是 `requires_approval` 布尔模型 | `app/agent/approval.py`, `app/util/menu.py` |
| 内置工具 | `read_file / write_file / list_dir / code_search / shell / todo_write`；`terminal_*` 交互式终端会话 | `app/tools/builtin/` |
| MCP | stdio Manager、工具发现、sync/async 桥、同名不覆盖、auto_approve 白名单 | `app/mcp/` |
| 浏览器控制 | Playwright MCP：打开/snapshot/点击/输入；named profile；数据边界提示 | `config/mcp_servers.example.yaml`, `app/cli.py` |
| 存储 | SQLite：sessions/messages/memories/tool_executions；尚无 runs/tasks/settings 表与 Web 复用 | `app/storage/` |
| 安全基础 | workspace 越界拒绝、脱敏、MCP env allowlist、敏感目录不入库 | `app/util/redact.py`, `.gitignore` |
| 测试 | 235 个 unit tests | `tests/unit/` |

---

## 5. 当前代码结构

```text
AgentLab/
  app/
    cli.py                         # CLI 入口、REPL、spinner、SessionRouter 接入、斜杠补全、数据边界提示
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
      runtime.py                   # AgentSession 工具循环
      approval.py                  # 审批策略
      tasks.py                     # 轻量 TaskStore
      profiles.py                  # AgentProfile + load_agent_profiles
      session_router.py            # SessionRouter + /session 命令族
    tools/
      registry.py                  # Tool 注册表（仍是 requires_approval 布尔模型）
      builtin/
        files.py                   # read_file / write_file / list_dir
        code_search.py             # 高频代码搜索
        shell.py                   # 跨平台 shell
        interactive.py             # 交互式终端会话(PTY)：PtySession + manager + terminal_* 工具
        todo.py                    # todo_write
    mcp/
      config.py                    # mcp_servers.yaml loader
      manager.py                   # stdio MCP 生命周期 + sync↔async 桥
      adapter.py                   # MCP tool -> Tool
    storage/
      __init__.py                  # SQLite：sessions/messages/memories/tool_executions
    memory/
      __init__.py                  # 记忆策略 + 注入
    skills/
      loader.py                    # 扫描 skills/*/SKILL.md，解析 frontmatter + 校验 metadata
      catalog.py                   # SkillCatalog：启用状态、按任务解析、上下文注入
    util/
      menu.py                      # 方向键菜单
      redact.py                    # 脱敏
  config/
    models.yaml                    # 模型 profile
    agents.example.yaml            # 多 Agent 模板（含 skills 字段示范）
    mcp_servers.example.yaml       # MCP 模板
  skills/
    code-review/
      SKILL.md                     # Skill 元数据 frontmatter + 工作流正文
      references/checklist.md      # 按需注入的参考资料
  docs/
    technical_architecture.md      # PRD 和总体技术方案
    process.md                     # 当前进展和接下来工作
  tests/unit/                      # 235 个 unit tests
```

尚未出现但 PRD 已规划的目录：`app/control/`、`app/server.py`、`app/web/`。

---

## 6. 模块进展与下一步

> 已完成的能力见第 3 节。本节各模块只列"当前状态 + 还要做什么 + 验收标准"。

### 6.1 Runtime 与任务拆解

当前状态：同步多轮工具循环可用；`TaskStore` 仅服务 `todo_write` 和 CLI 面板，结构较轻；没有显式编排和结构化事件。

接下来要做：

- 新增 `app/agent/planner.py`，把复杂用户目标拆成 `TaskPlan`。
- 新增 `app/agent/executor.py`，按依赖从 TaskStore claim 下一步任务并执行。
- 新增 `app/agent/replanner.py`，根据工具结果、错误、审批拒绝和用户追加目标调整任务。
- 升级 `tasks.py`，支持 dependencies、blocked、failed、evidence、history、snapshot。
- 定义结构化 `RunEvent`：`message_delta / tool_requested / approval_required / tool_completed / task_updated / run_completed / run_failed`。

验收标准：fake model 测试覆盖初始计划、按依赖执行、工具失败后重规划、审批拒绝后阻塞、用户追加目标、取消和 max_steps。

### 6.2 多 Agent、Session 与长期记忆

当前状态：核心功能已完成（见 3.4）。剩下的是非阻塞优化。

接下来要做：

- CLI banner / prompt 提示符显示当前 session_id 和 agent 名称。
- `read_write` 记忆策略的"会话结束写摘要"接入 CLI 退出钩子（store/policy 已实现，CLI 退出时尚未调用 `mem_policy.save`）。
- `memories` 升级为向量检索（当前 LIKE 全文匹配，够 MVP）。
- `/session list` 显示每个会话的消息数，方便辨认空会话。

### 6.3 模型层

当前状态：三种 adapter 可用；本地模型可用但不支持 tools 的模型能力不完整。

接下来要做：

- 启动时统一校验 `chat / tools / streaming / json_action` 能力。
- 为不支持原生 tools 但 JSON 稳定的模型补 `json_action_adapter`，默认限制高风险工具。
- 建立 Ollama、LM Studio、vLLM 兼容矩阵，记录工具调用、流式、上下文长度限制。
- 把云端数据边界提示从 CLI banner 升级为 RunEvent 级别，让 Web UI 也能显示。

验收标准：同一 AgentProfile 能在支持 tools 的云端模型和本地模型间切换；不支持 tools 的模型不会被误放行执行危险工具。

### 6.4 工具与审批

当前状态：`Tool` 只有 `requires_approval` 布尔字段；内置工具齐全；MCP 工具走 auto_approve 白名单。

接下来要做：

- 将 `Tool` 升级为 `ToolDescriptor`，补 `risk / target_type / scope / origin / host / requires_observation / audit_redactor`。
- 审批升级为分级策略：`read / observe / network / write / browser_control / desktop_control / remote_execute / execute / destructive`。
- 支持会话级授权（绑定 tool/origin/host/workspace）；删除、支付、发布、上传等动作不能被普通授权绕过。
- 内置工具、MCP 工具、浏览器动作、远程动作统一进入审计摘要。

验收标准：同一审批策略可同时判断 `write_file`、`shell`、`browser_click`、MCP tool 和 remote command。

### 6.5 MCP

当前状态：stdio Client 可用，Playwright 已接入（见 3.3）。

接下来要做：

- 增加 Streamable HTTP transport。
- MCP 工具映射到新版 `ToolDescriptor`，继承 server risk，可按工具提高风险等级。
- MCP 调用写入 `tool_executions` 审计表。
- 增加健康状态、断线重连、连接失败事件。

验收标准：stdio 和 Streamable HTTP 两种 MCP 都能通过统一 ToolRegistry 调用，并被同一套审批和审计策略处理。

### 6.6 Computer Control Gateway

当前状态：还没有 `app/control/`；浏览器控制直接通过 Playwright MCP 暴露为工具，没有统一 Gateway；桌面和远程控制未实现。

接下来要做：

- 新增 `app/control/sessions.py`，定义 `ControlTarget / ControlSession / Observation / ControlAction`。
- 新增 `app/control/gateway.py`，所有 browser/desktop/remote 动作经目标校验、风险判断、审批、执行、审计。
- 先把 Playwright MCP 包装成 browser backend，再考虑自建 Playwright Python adapter。
- 新增 `config/control.example.yaml`，声明 browser、desktop_control、remote_hosts。

验收标准：browser snapshot/click/type 都通过 ControlGateway 产生 observation、approval request 和 audit record。

### 6.7 浏览器控制

当前状态：Playwright MCP 可用，named profile 可保留登录态，云端数据边界已提示（见 3.3）。

接下来要做：

- 细化按 origin 的审批，登录、支付、授权、删除、上传、发布动作二次确认。
- 截图和 snapshot 统一落到受控 data 目录，并以 observation id 引用。
- 增加本地测试页面，覆盖点击、输入、导航、下载路径限制。
- 富文本/内部文档修改场景，优先设计 REST API/MCP 工具，不依赖脆弱 DOM 点击完成精确编辑。

验收标准：Agent 能在本地测试页完成打开、观察、点击、输入、提交前确认，并能在审计里回看关键动作。

### 6.8 远程设备控制

当前状态：已有**通用交互式终端会话**能力（见 3.2 / `app/tools/builtin/interactive.py`），可通过 `terminal_open("zsh -ic 'vsm <device>'")` + `terminal_send` 登录远程设备并逐条执行命令（已在真实 orangepi 设备上验证 open→命令→close 全链路）。这是 PTY 驱动的通用能力，ssh / REPL / 交互式程序同样适用，凭据留在本机命令配置里、不进工具参数。尚无"预配置 host 白名单 + 结构化 SSH Runner"那一层。

接下来要做：

- 新增远程 host / device 配置，按白名单限制 `terminal_open` 可连接的目标（当前不限制，靠 requires_approval 兜底）。
- 把交互式会话动作纳入审计（tool_executions），记录 device、命令、时间。
- 文件传输只允许本地 workspace 与 remote workspace 之间。
- 复杂远程 GUI 操作优先走远程 Agent Worker 或 Remote Host MCP。

验收标准：Agent 可在配置过的远程 host 指定 workspace 内执行只读命令；越界、未知 host、危险命令都被拒绝或要求强审批。（交互式登录+执行已可用，白名单与审计待补。）

### 6.9 Skill

当前状态：Skill Loader + Catalog 已完成（见 3.6）。`skills/*/SKILL.md` 扫描、metadata 校验、启用/禁用、按 query 触发匹配、按 AgentProfile 注入工作流均可用；Skill 只影响上下文不授予工具权限。剩下的是非阻塞增强。

接下来要做：

- 基于描述/embedding 的 Skill 自动推荐（当前仅 trigger 关键词匹配）。
- `/skill` 命令族：list / enable / disable / show，运行时查看和切换启用状态。
- 启用未知来源 Skill 前展示其 `allowed_tools` 和引用文件让用户确认（PRD §8.3.5，当前靠 frontmatter `enabled` 默认禁用兜底）。
- Skill `references/` 文件按需注入（当前只收集路径，未按需读入上下文）。

验收标准：一个 Coding Skill 可按 AgentProfile 启用并注入工作流说明；未授权工具仍不能被调用。（核心已满足，见 3.6 与 `tests/unit/test_skills.py`。）

### 6.10 存储、Web UI 与发布

当前状态：SQLite 已落 `sessions / messages / memories / tool_executions`（见 3.4）；尚无 `runs / tasks / settings` 表；没有 Web UI；CLI 与未来 Web 尚未抽出共用 Runtime service。

接下来要做：

- 补 `runs / tasks / settings` 表，把审计事件结构化落库。
- 抽出 Runtime service，让 CLI 与 Web UI 共用同一逻辑。
- 新增 `app/server.py` 和 `app/web/`，提供本地 Web UI、SSE 事件、审批 API、Stop 按钮。
- 配置面板能查看 AgentProfile、模型 profile、Skill、MCP Server、Control Target 和工具风险等级。

验收标准：退出重启后可 `/session list` 看到历史 session，并恢复消息、任务和记忆摘要（消息与记忆恢复已实现，任务恢复待 6.1）。

### 6.11 安全、可观测性与测试

当前状态：已有 workspace 限制、脱敏、MCP env allowlist、审批基础；测试以 unit 为主，集成测试目录存在但未成主路径。

接下来要做：

- API Key 从 `.env` 迁移到 macOS/Windows Keyring，`.env` 只做开发兜底。
- 所有 tool execution、approval、control action、model profile、actual model 进入审计事件。
- 增加 provider fake、MCP test server、本地浏览器测试页、fake SSH target。
- 高风险模块默认禁用，首次启用必须展示能力、数据边界和风险。

验收标准：一次包含模型推理、工具调用、审批、浏览器 observation 的 run 可被完整回放为事件和审计记录。

---

## 7. MCP 接入路线

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

## 8. 运行与验证

推荐环境是 conda 环境 `agentlab`，Python 3.11（MCP SDK 要求 ≥3.10）。

```bash
conda activate agentlab

# 收集测试。当前 235 个 unit tests。
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

多 Agent / 会话：

```bash
cp config/agents.example.yaml config/agents.yaml   # 启用多 Agent
# REPL 内:输入 / 看命令补全;
#   /session new coder 我的任务   新建并切到代码助手
#   /session list                 看全部会话
#   /session switch <id>          切换(可恢复历史)
#   /session delete <id>          硬删除(不可恢复)
```

Playwright MCP 浏览器控制：

```bash
cp config/mcp_servers.example.yaml config/mcp_servers.yaml
# 编辑 config/mcp_servers.yaml，把 playwright.enabled 改成 true。
python -m app --profile cloud_claude -p "打开 https://example.com 并告诉我页面标题"
```

注意：

- Playwright MCP 首次启动可能通过 `npx` 下载 server 和浏览器内核。
- named persistent profile 的登录态保存在 `data/browser-profiles/<name>`，不入库。
- 云端模型配合浏览器控制时，页面 DOM、截图摘要、表单内容可能进入云端模型上下文。

---

## 9. 接手注意事项

- 先读 PRD 的目标设计，再读本文件判断当前代码缺口。
- 做实现优先保持现有模式：Python dataclass、pytest unit test、fake provider/fake manager、workspace 限制、脱敏。
- 查代码优先用 `rg` 或内置 `code_search`，不要让模型通过 shell 拼复杂 grep/find。
- 修改 PRD 时只改目标设计；修改进度、完成情况、下一步计划时只改本文件，**完成项归到第 3 节而不是在下一步表里标记**。
- 高风险能力的顺序应是：先结构化描述和审计，再接真实执行能力。
