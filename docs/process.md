# AgentLab 进展与交接文档

本文档用于让新的 AI 或开发者快速接手项目：先看当前进展，再按 PRD 继续推进。总体目标和最终设计只维护在 [`technical_architecture.md`](./technical_architecture.md)，不要把阶段进展、更新时间或临时代码状态写进 PRD。

维护规则：

- `technical_architecture.md` 是目标方案和 PRD，只描述“最终应该是什么”。
- `process.md` 是当前进展和执行计划，必须始终写清楚“现在做到哪里、还缺什么、下一步先做什么”。
- **完成的工作统一归到第 3 节「已完成里程碑」**，不要在各模块的"下一步"表里用删除线或"已完成"标记堆积；模块章节只保留真正待做的事。
- 「当前需要做的事情」最多保留 5 项，避免变成无限待办清单。

---

## 1. 当前一句话状态

AgentLab 是一个可运行的本地 CLI Agent：支持模型 profile 切换、云端/本地 adapter、流式输出、多轮工具调用、ToolDescriptor 九级风险与统一工具审计、方向键审批、内置文件/代码搜索/web 搜索/shell/交互式终端/todo 工具、stdio MCP Client（Playwright 浏览器控制）、多 Agent `/session` 切换、SQLite 持久化与长期记忆、Skill Loader（按 AgentProfile 注入工作流上下文）、`Planner + Executor + Replanner` 编排路径（带依赖 TaskStore + 结构化 `RunEvent`，已接入 CLI 主路径，支持 Ctrl-C 协作式取消与任务状态持久化恢复）、上下文预算与自动压缩（`ContextBudget` + `ContextCompressor` + 结构化摘要 + `/context` 命令族，编排稳定点超阈值自动压缩、可审计）。

**Loop 模式（Loop Engineering，§7.6）的证据闭环已接通**：`/goal new` 定义目标和验证命令，`/loop start` 创建隔离 worktree，调用 `Orchestrator` 执行 Planner→Executor→Replanner，再由 command/file/API/Human Verifier 验证；失败时生成修复指令继续迭代，成功时经审批生成可合并提交。run、iteration、任务快照、验证结果、worktree、repair/diff/commit、预算和终止原因完整持久化，可用 `/loop evidence`、`/loop diff` 回看，并用 `/loop resume` 恢复未完成运行。尚缺 browser/database/remote/llm_judge Verifier、`Learner` / Project Knowledge、子 Agent 和后台 Loop。

距离 PRD 的核心缺口：Loop Engineering 的学习与协作能力尚未完成；Runtime Service、异步 Approval Broker 和 Loop 持久化证据闭环已完成并接入 CLI，但还没有自建 Computer Control Gateway、FastAPI Server、终端 TUI 和 Web UI。互联网能力目前仍是单工具形态，尚无统一 WebRetrievalService、Source Store 和 CitationManager。

---

## 2. 接下来开发计划（按优先级，最多 5 项）

以下顺序遵循“先解耦运行时，再开放新入口；先形成证据与安全边界，再增加执行能力”。每一项完成后都应更新第 3 节里程碑，并保持 CLI 行为兼容。

1. **P1：建立 ComputerControlGateway 并接入 browser Verifier**（见 6.6、6.7）
   - 先包装现有 Playwright MCP，将 snapshot/click/type/navigation 规范化为 Observation 和 Action，不让 Runtime 依赖 MCP 工具的原始返回结构。
   - 在网关统一执行目标校验、风险分级、审批、敏感字段处理、截图/DOM 限长、审计和取消。
   - 基于 Observation 实现 browser Verifier，支持 URL、可见文本、元素状态和截图证据检查。
   - **验收**：浏览器写动作无法绕过 Approval Broker；页面内容不能修改权限或 GoalSpec；使用本地测试页面完成离线端到端验证。

2. **P1：实现 FastAPI Server MVP 与 SSE 事件流**（见 6.10）
   - 基于 Runtime Service 提供 session、message/run、approval、cancel、loop evidence API，以及可重连的 SSE 事件端点。
   - 增加 `agentlab serve`，仅默认监听 loopback；定义请求 ID、错误模型、事件序号、断线与关闭语义。
   - 先提供最小管理页验证聊天、工具进度和审批交互，不在此阶段扩展完整 Web UI/TUI。
   - **验收**：CLI 与 API 复用同一 runtime 路径；两个 session 并发不串上下文/审批；API、SSE 重连、取消和安全默认值有集成测试。

3. **P1：实现受控互联网检索与引用骨架**（见 6.15）
   - 抽出 `WebRetrievalService`、可替换 `SearchProvider`、受控 Fetcher、Source/Document Store 和 `CitationManager`；现有 `web_search/web_fetch` 降为薄工具适配器。
   - 统一 URL 规范化、SSRF 防护、重定向检查、缓存、来源去重、获取时间、正文定位、截断和 prompt injection 数据边界。
   - 为最终回答提供可校验 citation，禁止引用未实际抓取或无法映射到 source/document 的链接。
   - **验收**：fake provider 可离线覆盖搜索→抓取→引用全链路；引用能追溯 URL、获取时间和正文片段；现有 Web 安全测试保持通过。

执行约束：每项按“接口/数据模型 → fake 与单元测试 → 最小实现 → CLI/API 集成 → 文档与跨平台回归”推进；不要同时启动后续 P1 大模块，直到 P0 的 Runtime Service 和证据模型接口稳定。

---

## 3. 已完成里程碑

> 完成的能力统一记在这里。各模块章节（第 5 节）只描述"当前状态 + 还要做什么"，不再重复罗列已完成项。

### 3.1 P0 核心 CLI Agent

- CLI REPL：交互模式、单次 `-p/--prompt`、`--profile`、`-y` 自动审批；prompt_toolkit 输入框、spinner 进度（`✻ thinking… (3.2s · ↓ 42 tokens)`）、token/耗时统计。
- 模型层：Anthropic Messages、OpenAI Responses、OpenAI-compatible 三种 adapter；内部协议统一 `ModelResponse / ToolCall / ToolResult`；OpenAI-compatible 具备 JSON tool call fallback；实际模型 ID 规范化 + 代理静默映射提示。
- Runtime：同步多轮"模型 → 工具 → 模型"循环；工具审批、工具错误回灌、流式文本回调；`max_steps` 作为 run 级模型往返总预算，`max_task_steps` 限制单个子任务，二者共同防止空转与单任务独占。
- 审批：`AutoApprove / InteractivePolicy(方向键菜单) / DenyAll`。
- 安全基础：workspace 内按风险执行、越界使用不可持久化的独立审批动作、错误/工具输出脱敏（`redact`）、MCP env allowlist。
- 图片附件：CLI 支持 `/image`、`/paste-image` 和输入框 Ctrl+V/Shift+Insert 直接粘贴；`AttachmentStore` 校验 MIME/大小/像素/workspace 审批并落到 `data/attachments/<session>`，消息历史只存 file/hash 元数据，OpenAI Responses、Anthropic 与 Chat Completions adapter 在请求前分别物化为对应图片 block；profile 必须声明 `vision`。
- 内置 RTK 输出压缩：`shell` 执行后按 git/test/grep/listing/diagnostics/container 类别过滤、分组、截断和去重；无需外部 RTK 二进制，失败或收益不足自动回退原始输出，保留审批、cwd、timeout、stderr 和退出码；`/rtk` 查看状态。

### 3.2 内置工具

- `read_file / write_file / list_dir`：workspace 是默认信任边界；workspace 内只读免审批，写入按原风险审批，越界改用独立且不可记忆的审批动作。`shell` 默认 cwd 为 workspace、每次审批，指定外部 cwd 时使用独立越界审批。`todo_write` 提供 CLI 任务面板 `✓/❯/○`。
- `code_search`（`app/tools/builtin/code_search.py`）：text/regex/file/symbol 四种模式，优先 `rg --json`，无 rg 时 Python fallback；遵守 `.gitignore`、命中行密钥脱敏；workspace 内只读免审批，外部搜索逐次审批。
- `web_search`（`app/tools/builtin/web_search.py`）：互联网搜索工具，返回结构化结果（标题/URL/摘要）。优先用 `duckduckgo_search` 库，失败时退化为 requests + BeautifulSoup 解析 DuckDuckGo HTML；结果脱敏、超时保护、输出 32KB 硬截断；只读免审批；依赖（`duckduckgo-search` / `requests` / `beautifulsoup4`）未安装时优雅降级返回安装提示。完全跨平台（无路径操作/subprocess/POSIX 特定功能）。22 个单元测试（16 passed + 6 skipped，跳过项为可选依赖未装）。
- `web_fetch`（`app/tools/builtin/web_fetch.py`）：给定 URL 抓取网页正文并转 Markdown。HTTP GET 抓 HTML → 正文抽取（trafilatura 最优 → readability → BeautifulSoup 兜底）→ Markdown 转换；只允许公网 http/https，拒绝 URL 凭据、本机/私网/链路本地/保留地址和解析到非公网 IP 的域名；关闭自动重定向并逐跳重新校验，阻断重定向 SSRF；响应体 5MB 上限 + 正文 20K 字符截断；只读免审批；依赖未装时优雅降级。30 个单元测试全通过。
- 交互式终端会话（`app/tools/builtin/interactive.py`）：`PtySession` 在伪终端里起子进程，`read-until-idle` 通用驱动（不依赖提示符/哨兵）；`terminal_open / terminal_send / terminal_close / terminal_list` 四个工具按会话注入；用于远程登录（`zsh -ic 'vsm <device>'`、ssh）、REPL、交互式安装器等 `shell` 搞不定的有状态会话。子进程随 AgentSession 关闭而清理。

### 3.3 MCP Client 层（stdio）+ Playwright 浏览器控制

- `app/mcp/`：`config.py`（读 `mcp_servers.yaml`）、`manager.py`（stdio 生命周期 + sync↔async 调用桥）、`adapter.py`（MCP tool → 内置 Tool）。
- 工具发现后映射为 Tool；同名工具不覆盖内置；`auto_approve` 白名单中的只读工具免审批，其余需审批。
- Playwright MCP 已接入：打开页面、snapshot（无障碍树）、点击、输入。
- Windows stdio 启动已兼容：同一份 `command: npx` 配置会自动解析
  `npx.cmd`；子进程获得 `SYSTEMROOT / COMSPEC / PATHEXT / TEMP /
  USERPROFILE / APPDATA` 等必需的非敏感环境变量，额外变量仍受
  `env_allowlist` 控制；MCP SDK 最低版本为 1.27.2。
- named persistent profile：可保留登录态（`--user-data-dir`，profile 目录不入库）。
- MCP 的 `cwd` 支持相对项目根目录解析，Playwright profile 路径不再依赖
  启动 AgentLab 时所在的终端目录。
- 云端数据边界提示：云端模型 + 浏览器 MCP 时，CLI 启动提示页面 DOM/截图/表单会进入云端模型上下文。

### 3.4 多 Agent、Session 切换与长期记忆

- `app/agent/profiles.py`：`AgentProfile`（agent_id/name/model_profile/system_prompt/tools/mcp_servers/memory_policy/max_steps/max_task_steps）+ `load_agent_profiles()` 读 `config/agents.yaml`。
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
- `pyproject.toml` 提供可编辑安装和 `agentlab` console script；CLI
  `-w / --workspace` 支持从任意目录选择 Agent 工具边界，显式参数优先于
  `WORKSPACE_ROOT`，`python -m app` 开发入口保持兼容。

### 3.6 Skill Loader

- `app/skills/loader.py`：扫描 `skills/*/SKILL.md`，解析 YAML frontmatter + 工作流正文；校验 metadata（缺 name/description 视为无效，静默跳过）；`skill_id` 取目录名（稳定，不随 frontmatter 漂移）；附带 `references/` 文件路径。
- `app/skills/catalog.py`：`SkillCatalog` 管启用状态（默认取 frontmatter `enabled`，可 `enable/disable`）、`resolve()` 按 `AgentProfile.skills` + 本轮 query 选 Skill、`build_skill_context()/inject()` 把工作流拼进 system prompt。
- 安全边界：Skill 只影响上下文，`allowed_tools` 是"需求/上限"非授权；注入文本显式声明"不授予工具权限"；未知来源 Skill（无 `enabled: true`）默认禁用，需被 `AgentProfile.skills` 显式引用或显式启用才注入。
- `AgentProfile` 新增 `skills` 字段；CLI 启动扫描 catalog 并打印发现/启用数，`_session_factory` 在记忆注入之前注入 Skill 工作流（不放宽工具集）。
- 模板：`skills/code-review/`（SKILL.md + references/checklist.md）；`config/agents.example.yaml` 的 coder 示范 `skills: [code-review]`。

### 3.7 测试

- **547 个 unit tests**（全离线），覆盖：runtime（含动态审批、编排委托 + 取消）、Orchestrator/Planner/Executor/Replanner 编排路径、TaskStore（依赖/claim/状态回写/snapshot/restore）、上下文预算与压缩（token 估算/预算阈值/安全选段/摘要校验脱敏/ContextManager/storage）、三种 adapter、ToolDescriptor/九级风险/结构化审批/统一审计/旧数据库迁移、MCP（config/adapter/manager，含 Windows `npx.cmd`、最小运行环境和 cwd）、CLI 全局入口与 workspace 参数、code_search（含外部目录审批）、web_search、web_fetch（公网地址校验、DNS/重定向 SSRF、正文抽取、截断、脱敏）、shell（含外部 cwd 审批）、交互式终端会话、workspace path、存储、记忆、session_router、Skill loader/catalog、Loop Engineering（真实多轮编排、Verifier 审批、worktree 相对路径/未跟踪文件/审批提交与合并边界、执行异常终止）。
- `.github/workflows/mcp-cross-platform.yml` 在 Windows、Linux、macOS
  runner 安装 Node.js 后真实验证 `npx` 解析，并运行 MCP 专项测试。

### 3.8 Planner / Executor / Replanner 编排与结构化 RunEvent

- `app/agent/tasks.py`：`TaskStore` 升级为任务状态唯一来源。`Task` 新增 `dependencies / evidence / error / history`；状态增 `blocked / failed`（编排路径专用,`todo_write` 仍只写简单三态）。新增 `add / extend / get / update_status / claim_next（按依赖）/ has_runnable / has_open / is_done / is_stalled / snapshot`。`summary()` 向后兼容(保留 total/pending/in_progress/completed,加 blocked/failed)。
- `app/agent/events.py`：结构化 `RunEvent`，kind 覆盖 `run_started / plan_created / task_started / message_delta / tool_requested / approval_required / tool_completed / tool_denied / task_updated / run_completed / run_failed`;`payload.tasks` 携带任务 snapshot。
- `app/agent/planner.py`：`Planner` 让模型只输出 JSON 计划,`_extract_json` 容忍 markdown 围栏/前后散文,`_parse_tasks` 跳过非法项并去重 id;解析失败或模型异常时退化为单任务计划,保证永不卡死。
- `app/agent/executor.py`：`Executor.run_task` 按"单个子任务"驱动有限轮模型/工具循环，把任务指令注入共享 messages，产出 `TaskOutcome(completed/failed/blocked)`；显式记录 `model_rounds` 与 `tool_calls_made`，工具出错→failed，审批被拒→blocked，单任务轮次耗尽→failed；发 `RunEvent`，不直接写 TaskStore。
- `app/agent/replanner.py`：`Replanner.apply` 把 `TaskOutcome` 回写 TaskStore;失败任务追加一次"复查并修复"补救任务(重试任务再失败不再追加,避免无限循环),阻塞不追加。
- `app/agent/orchestrator.py`：`Orchestrator.run(goal)` 串联规划→claim→执行→重规划；`max_steps` 兼容旧配置名但语义明确为 run 级模型往返总预算，按 `TaskOutcome.model_rounds` 扣减（纯文本轮次也计数），`max_task_steps` 单独限制一个子任务，避免首任务独占全部预算；同时支持协作式取消。多次 `run()` 在已有任务之上追加新计划（任务 id 加 `rN-` 前缀避免跨 run 撞车），收工/卡死/取消/超预算分别发对应 RunEvent。
- 测试 `tests/unit/test_orchestrator.py`(覆盖 §6.1 验收:初始计划、按依赖执行、工具失败后重规划、审批拒绝后阻塞、用户追加目标、取消、max_steps)。

### 3.9 编排路径接入 CLI + 任务状态持久化

- `app/agent/runtime.py`：`AgentSession` 新增 `orchestrate / planner / on_run_event` 参数与 `chat(cancel=...)`。`orchestrate=True` 时 `chat` 委托给懒构建的 `Orchestrator`(共享同一份 `messages` 与 `task_store`),把 run 级 token 用量 / actual_model / `last_run_status` 拷回 session;`orchestrate=False`(默认)仍走原 legacy 单轮循环,既有 runtime 测试行为不变。`reset()` 改为就地 `messages.clear()`,保持 Orchestrator 共享引用有效。
- `app/agent/executor.py` / `planner.py` / `orchestrator.py`：补 progress(spinner)与 token 用量回传 —— Executor 每轮 `create_message` 包进 progress CM 并支持流式 `on_text_delta`、累加 `usage_acc`、回传 actual_model;Planner 暴露 `last_usage`/`last_actual_model`;Orchestrator 规划阶段也走 progress,汇总 `last_run_usage` 与 `last_run_status`,并接受外部传入的共享 `messages`。
- `app/cli.py`：`_session_factory` 用 `orchestrate=True` + `Planner(llm)` + `on_run_event=_print_run_event` 装配 session。新增 `_print_run_event` 把 RunEvent 映射到终端(message_delta 流式补打、tool_* 工具行、plan_created/run_completed/run_failed 打任务面板与原因);`_format_task_lines` 兼容 Task 对象与 snapshot dict,新增 blocked(⊘黄)/failed(✗红)字形。新增 `_chat_with_cancel`:装 SIGINT 处理器,首次 Ctrl-C 置 `CancelToken`(当前步骤后干净停止),连按强制中断;`_repl` 与单次 `-p` 均走该包装。
- `app/storage/__init__.py`：新增 `runs / tasks` 两表 + `save_tasks / load_tasks`(整表覆盖,按 position 排序,evidence/error 脱敏)、`log_run / list_runs`;`delete_session` 一并清 tasks/runs。`app/agent/tasks.py` 新增 `TaskStore.restore(snapshot)`(就地重建,不换引用)。
- `app/agent/session_router.py`：`switch` 从 SQLite 恢复消息后再 `restore` 任务快照;`persist_current` 同时存消息 + 任务快照,并在 session 记录了 goal/run_status 时写一条 runs 审计。
- 测试:`test_runtime.py`(编排委托拷统计、取消、legacy 默认不受影响)、`test_storage.py`(tasks/runs round-trip、覆盖、脱敏、删除连带清理)、`test_session_router.py`(switch 恢复任务快照)。

### 3.10 上下文构建与压缩(§7.3 / §6.13)

- `app/agent/context_budget.py`:`estimate_tokens`(CJK 友好、偏保守的离线字符启发式,不依赖 tokenizer/网络)、`resolve_context_limit`(profile 声明的 context_size 优先,否则按模型名前缀查表,未知退 8192)、`ContextBudget.from_model`(派生 reserved_output / system_and_tools / memory / summary / recent / evidence 各项预算)、70% 警告 / 85% 强制压缩阈值与 `status_for`。
- `app/agent/context_compaction.py`:`ContextSummary`(§7.3.3 结构化字段 + source_message_range/source_run_ids/token_before/after/compression_model_profile 审计元数据)、`ContextCompressor`:`_safe_split_point` 选段(保留最近窗口 + 不切开 tool_use/tool_result 对,避免悬空 tool_call)→ 调模型产出结构化 JSON 摘要 → 必填字段校验(user_goal/handoff_note)+ 递归脱敏 → 用一条摘要消息就地顶掉旧前缀;摘要非法/模型异常/无可压段时不压缩(保留原始尾部)。
- `app/agent/context.py`:`ContextManager` 黏合 budget + compressor。`maybe_compact` 在稳定点检查预算:ok 不动 / warn 发 `context_budget_warning` / compact 触发压缩并发 `context_compaction_started/completed/failed`;`report()` 供 `/context` 展示预算+recent window+摘要状态;`drain_records()` 把压缩摘要交给持久化;`force=True` 供 `/context compact` 手动压缩;`auto_compact` 开关。
- `app/agent/events.py`:新增 `context_budget_warning / context_compaction_started / context_compaction_completed / context_compaction_failed` 四个 RunEvent kind。
- `app/agent/orchestrator.py`:新增可选 `context_manager`,在规划后与每个任务完成后(稳定点,此时 tool 对已闭合)调 `_maybe_compact`;压缩异常一律吞掉,绝不中断主任务;无 context_manager 时行为完全不变。`AgentSession` 透传 `context_manager` 给 Orchestrator。
- `app/storage/__init__.py`:新增 `context_summaries` 表(summary_json / range_start/end / source_run_ids / token_before/after / compression_model_profile / created_at)+ `save_context_summary / list_context_summaries / latest_context_summary`,写入再兜底脱敏;`delete_session` 连带清理。`SessionRouter.persist_current` 把本轮 `drain_records()` 的摘要 flush 落库(原始消息仍由 save_messages 保留,压缩只换模型输入里的旧片段)。
- `app/cli.py`:`_session_factory` 按 `cfg.context_size`/模型窗口构建 `ContextManager` 装配进 session;`_print_run_event` 渲染 context_* 事件(警告百分比 / 压缩前后 token);`/context` 命令族(看预算 / `compact` 手动压缩 / `summary` 看摘要 / `disable-auto-compact` / `enable-auto-compact`)+ 斜杠补全二级子命令。
- 测试 `tests/unit/test_context.py`(33 个):token 估算、窗口解析、预算阈值、安全选段(含不切 tool 对)、摘要解析/校验/脱敏、ContextManager 触发与不触发/禁用/force/drain/report、storage round-trip + 脱敏 + 删除清理、Orchestrator 稳定点压缩 + 无 context_manager 向后兼容。

### 3.11 Loop Engineering 基础闭环（已接入 CLI）

- **GoalSpec** (`app/agent/goals.py`)：目标定义 dataclass（objective/success_criteria/constraints/budgets/verification_plan/stop_conditions/workspace_mode/learning_policy）+ `validate()` 校验（objective 非空、success_criteria 可验证、至少一个 verifier、高风险禁止只用 llm_judge、预算 > 0）+ `is_valid()` 快速判断。10 个单元测试覆盖所有校验规则。
- **Verifier** (`app/agent/verifier.py`)：验证器统一入口 + `VerificationResult` / `CheckResult` 结构化输出。已实现 `command`（审批后使用 macOS/Linux bash 或 Windows PowerShell 检查 exit code）、`file_assertion`；缺失审批策略或用户拒绝时返回 blocked，不再使用 `shell=True` 绕过审批。未实现类型（browser/api/database/remote/human/llm_judge）返回 blocked。20 个单元测试覆盖命令执行、审批拒绝、文件断言和多检查组合。
- **LoopRunner 状态机** (`app/agent/loop_runner.py`)：已真实调用 Orchestrator，并用 `use_workspace_root` 把文件、代码搜索和 shell 工具切入 worktree；传递 CancelToken、累计工具调用数、按验证失败结果生成修复轮。执行器异常进入 `loop_failed`，不再继续验证；成功后展示完整改动，并通过 `worktree_commit` 审批生成验证提交。待完善 Learner 和完整证据持久化。
- **WorktreeManager** (`app/workspace/worktree.py`)：Git worktree 隔离工作区生命周期管理。diff summary 使用 `git status --short` 覆盖未跟踪文件；`commit_all()` 在审批后提交全部改动；只有 worktree clean 且存在 base 之后的新提交才给出 merge 建议。14 个单元测试覆盖创建/删除/diff/untracked/dirty/commit/merge 安全边界。
- **loop_store** (`app/storage/loop_store.py`)：6 张新表（goal_specs / loop_runs / loop_iterations / verification_results / worktrees / subagent_runs）+ 索引；`save_goal_spec / load_goal_spec / save_loop_run / load_loop_run / save_verification_result / save_worktree`；所有写入经 `redact()` 脱敏，大内容存 blobs 只留引用。
- **Loop RunEvent** (`app/agent/events.py`)：14 个 Loop 生命周期事件，新增明确的 `loop_failed`。
- **Loop CLI 命令** (`app/agent/loop_commands.py`)：已接入 CLI 主循环；`/goal new <目标> :: <验证命令>` 创建并持久化 GoalSpec，`/loop start/status/stop` 驱动当前 session 的 Orchestrator、Verifier 和 WorktreeManager。

### 3.12 Runtime Service 与异步 Approval Broker

- `app/agent/service.py`：新增前端无关的 `RuntimeService`，统一提供 session 创建/恢复/切换、消息执行、指定 session 持久化、协作式取消、事件订阅和幂等资源关闭；AgentSession 的 TurnEvent/RunEvent 会带 session_id/run_id 汇入统一事件总线；同一 session 禁止并发 run，不同 session 可并行执行。
- `app/agent/approval_broker.py`：新增结构化 `ApprovalRequest`、稳定 request ID、pending 查询、订阅、approve/deny、超时安全拒绝与 close 唤醒；首个前端决定生效，后续重复回应不会覆盖。
- `BrokerApprovalPolicy` 保留现有同步 `ApprovalPolicy` 接口：CLI 方向键菜单和 `-y` 作为 fallback 经过 Broker 执行，未来 HTTP/TUI 可不设 fallback，异步回应 pending request。
- CLI 主消息路径、`/resume`、单次 `-p`、session 命令和退出清理已迁移到 RuntimeService；`SessionRouter.persist(session_id)` 避免不同 session 并发完成时写错持久化目标。
- 新增 `test_approval_broker.py` 与 `test_runtime_service.py`，覆盖并发 session、同 session 互斥、取消、超时、首决定生效、asyncio 非阻塞桥和资源关闭；同时修正 `web_search` 对新版 `ddgs` 与旧版 `duckduckgo_search` 的兼容测试，当前 547 项 unit tests 全部通过。

### 3.13 ToolDescriptor、分级审批与统一工具审计

- `app/tools/registry.py`：正式引入 `ToolDescriptor`，统一描述 `risk / target_type / scope / origin / host / requires_observation / audit_redactor`；`Tool` 保留为兼容别名，现有 Skill、测试和扩展无需一次性迁移。
- 九级风险分类已落地：`read / observe / network / write / browser_control / desktop_control / remote_execute / execute / destructive`。工具未显式覆盖 `requires_approval` 时由风险等级决定默认审批；workspace 越界仍由参数级 `approval_resolver` 提升为独立审批动作。
- `InteractivePolicy` 已能展示风险、目标、来源和 host；browser/desktop/remote/execute/destructive、shell/terminal 以及 workspace 越界动作不可使用“本会话总是允许”。旧 `ApprovalPolicy.request(action,args)` 通过兼容适配继续可用。
- 内置文件、代码搜索、Web、Shell、Todo、交互式终端和 MCP 工具均声明风险与目标元数据。MCP 工具继承 Server risk，并标注 `origin=mcp`、server host 和 browser observation 要求。
- `ToolRegistry` 对 completed/error/denied/approval_required 统一产出 `ToolAuditEvent`；CLI 为每个 Session 注入审计 sink，写入 SQLite `tool_executions`。表已补风险、目标、来源、host、审批动作、结果状态和 observation 字段，并可自动迁移旧数据库。
- 文件内容、代码搜索结果和网页正文使用工具级 `audit_redactor` 只记录有界摘要；MCP 协议错误不再伪装成功，而是作为工具错误回灌模型并进入审计。

### 3.14 Loop Engineering 持久化证据闭环

- `LoopRunner` 已把 run、每轮 iteration、TaskStore 快照、VerificationResult、预算消耗、终止原因、worktree 状态、修复计划、diff 和验证提交持续写入 SQLite，不再只在内存和终端事件中存在。
- `loop_store` 新增有界且脱敏的 `loop_artifacts` 制品表，并补齐 iteration/artifact/evidence/diff/list API；旧数据库启动时自动迁移 `termination_reason` 字段。
- `/loop evidence [loop_id]` 可回看状态、预算、终止原因、逐轮验证和制品；`/loop diff [loop_id]` 可读取持久化 diff；`/loop resume [loop_id]` 可恢复 cancelled 或进程中断留下的未完成 Loop，并继承迭代与工具预算。
- Verifier 新增经过审批的 API 检查（method/status/response contains）和真正可交互的 Human 检查；无交互器或无审批策略时安全返回 blocked。
- 新增 `test_loop_evidence.py` 并扩展 Verifier 测试，覆盖成功证据、任务快照、预算、脱敏/限长制品、diff 查询、恢复状态、API 审批和 Human 决策；当前 553 项 unit tests 全部通过。


| 模块 | 当前进展 | 关键文件 |
|---|---|---|
| CLI | 交互 REPL、`-p`、`--profile`、`-y`、`/reset`、`/session` 命令族 + 斜杠补全；spinner、任务面板、统计 | `app/cli.py` |
| 配置 | `.env` + `config/models.yaml`（模型）+ `config/agents.yaml`（Agent）+ `config/mcp_servers.yaml`（MCP） | `app/config/`, `app/agent/profiles.py`, `app/mcp/config.py` |
| 模型层 | Anthropic / OpenAI Responses / OpenAI-compatible；统一内部协议；JSON tool call fallback | `app/models/` |
| Runtime | CLI 主路径已切到编排:`AgentSession(orchestrate=True)` 委托 Orchestrator;`orchestrate=False` 仍保留 legacy 单轮循环 | `app/agent/runtime.py`, `app/agent/orchestrator.py` |
| 编排 | Planner(JSON 计划) + Executor(单任务工具循环,带 progress/流式/用量) + Replanner(失败追加补救) + CancelToken + 结构化 RunEvent;已接 CLI（PRD 的 Task 模式） | `app/agent/planner.py`, `app/agent/executor.py`, `app/agent/replanner.py`, `app/agent/events.py`, `app/agent/cancel.py` |
| Loop Engineering | GoalSpec → Orchestrator → Verifier → 诊断修复 → worktree 审批提交；run/iteration/verification/worktree/repair/diff/commit/预算/终止原因完整持久化；支持 `/loop evidence/diff/resume`；Learner、Project Knowledge、子 Agent 待做（见 6.14） | `app/agent/goals.py`, `verifier.py`, `loop_runner.py`, `loop_commands.py`, `app/workspace/worktree.py`, `app/storage/loop_store.py` |
| 多 Agent / Session | AgentProfile + SessionRouter + `/session` 命令族；SQLite 持久化、恢复、隔离（含任务快照恢复） | `app/agent/profiles.py`, `app/agent/session_router.py`, `app/storage/` |
| 长期记忆 | none/read/read_write 三策略 + 注入；LIKE 检索（未做向量） | `app/memory/` |
| Skill | Loader + Catalog：扫 `skills/*/SKILL.md`、按 AgentProfile 注入工作流；只影响上下文不授权 | `app/skills/`, `skills/` |
| 任务面板 / TaskStore | 任务状态唯一源:依赖/claim/blocked/failed/evidence/history/snapshot/restore;`todo_write` 走简单三态;CLI 面板渲染(含 blocked/failed 字形) | `app/agent/tasks.py`, `app/tools/builtin/todo.py` |
| Runtime Service | session/run 门面、事件订阅、协作式取消、同 session 互斥、跨 session 并发和统一资源关闭；CLI 已迁移 | `app/agent/service.py`, `app/cli.py` |
| 审批 | 异步 ApprovalBroker + 稳定 request ID/超时/首决定生效；CLI fallback；`ToolDescriptor` 九级风险与旧 Policy 兼容 | `app/agent/approval_broker.py`, `app/agent/approval.py`, `app/tools/registry.py` |
| 内置工具 | `read_file / write_file / edit_file / list_dir / code_search / web_search / web_fetch / shell / todo_write`；`terminal_*` 交互式终端会话 | `app/tools/builtin/` |
| MCP | stdio Manager、工具发现、sync/async 桥、同名不覆盖、auto_approve 白名单；映射 ToolDescriptor 并继承 Server 风险，调用进入统一审计 | `app/mcp/` |
| 浏览器控制 | Playwright MCP：打开/snapshot/点击/输入；named profile；数据边界提示 | `config/mcp_servers.example.yaml`, `app/cli.py` |
| 存储 | SQLite：sessions/messages/memories/tool_executions/runs/tasks/context_summaries + Loop 表；工具审计含风险/目标/来源/审批/结果；settings 表待做 | `app/storage/` |
| 安全基础 | workspace 默认信任边界、越界强审批、审批上下文防模型伪造、脱敏、MCP env allowlist、敏感目录不入库 | `app/tools/registry.py`, `app/util/redact.py`, `.gitignore` |
| 取消 | Ctrl-C 协作式取消(CancelToken):首次置位、当前步骤后停止,连按强制中断 | `app/agent/cancel.py`, `app/cli.py` |
| 上下文压缩 | ContextBudget + ContextCompressor + 结构化摘要;编排稳定点超 85% 自动压缩、可审计;`/context` 命令族 | `app/agent/context.py`, `context_budget.py`, `context_compaction.py` |
| 测试 | 完整离线 unit tests 通过（数量见 3.7）；三平台 MCP 专项 CI 已配置 | `tests/unit/`, `.github/workflows/mcp-cross-platform.yml` |

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
      runtime.py                   # AgentSession：legacy 单轮循环 + orchestrate 委托 Orchestrator
      orchestrator.py              # Orchestrator：串联 Planner/Executor/Replanner + 步数预算 + 取消
      planner.py                   # Planner：目标 -> JSON 计划 -> TaskPlan(带依赖);失败退化为单任务
      executor.py                  # Executor：单任务工具循环 -> TaskOutcome(completed/failed/blocked)
      replanner.py                 # Replanner：TaskOutcome 回写 TaskStore;失败追加补救任务
      events.py                    # 结构化 RunEvent(run/plan/task/tool/approval 生命周期)
      cancel.py                    # CancelToken：协作式取消
      approval.py                  # 审批策略
      tasks.py                     # TaskStore：任务状态唯一源(依赖/claim/blocked/failed/evidence/snapshot/restore)
      profiles.py                  # AgentProfile + load_agent_profiles
      session_router.py            # SessionRouter + /session 命令族
      context.py                   # ContextManager:稳定点预算检查 + 按需压缩 + 审计记录 flush
      context_budget.py            # 上下文预算:模型窗口解析/token 估算/各项预算/70%-85% 阈值
      context_compaction.py        # 上下文压缩:安全选段/结构化摘要/校验脱敏/就地替换旧历史
      goals.py                     # [Loop] GoalSpec + 校验(objective/success_criteria/verifier/预算)
      verifier.py                  # [Loop] Verifier:command/file_assertion + VerificationResult
      loop_runner.py               # [Loop] LoopRunner 状态机框架(执行→验证→诊断→修复)
      loop_commands.py             # [Loop] LoopCommandHandler:/goal /loop 命令处理(已接 CLI)
    workspace/                     # [Loop] 工作区隔离与项目知识
      worktree.py                  # WorktreeManager:git worktree 创建/diff/dirty/删除/合并建议
    tools/
      registry.py                  # ToolDescriptor、九级风险、动态审批、统一 ToolAuditEvent
      builtin/
        files.py                   # read_file / write_file / list_dir
        code_search.py             # 高频代码搜索
        web_search.py              # 互联网搜索(DuckDuckGo + HTML fallback,可选依赖优雅降级)
        shell.py                   # 跨平台 shell
        interactive.py             # 交互式终端会话(PTY)：PtySession + manager + terminal_* 工具(Windows 优雅降级)
        todo.py                    # todo_write
    mcp/
      config.py                    # mcp_servers.yaml loader
      manager.py                   # stdio MCP 生命周期 + sync↔async 桥
      adapter.py                   # MCP tool -> Tool
    storage/
      __init__.py                  # SQLite：sessions/messages/memories/tool_executions/runs/tasks/context_summaries
      loop_store.py                # [Loop] 6 表:goal_specs/loop_runs/loop_iterations/verification_results/worktrees/subagent_runs
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
  tests/unit/                      # unit tests（当前数量见 3.7，避免多处数字漂移）
```

尚未出现但 PRD 已规划的目录/模块：`app/agent/learner.py`、`app/agent/subagents.py`、`app/workspace/knowledge.py`、`app/workspace/scheduler.py`、`app/retrieval/`、`app/control/`、`app/tui/`、`app/server.py`、`app/web/`。（Loop 核心 `goals.py` / `verifier.py` / `loop_runner.py` / `loop_store.py` / `app/workspace/worktree.py` 已落地，见 3.11。）

---

## 6. 模块进展与下一步

> 已完成的能力见第 3 节。本节各模块只列"当前状态 + 还要做什么 + 验收标准"。

### 6.1 Runtime 与任务拆解

当前状态：编排核心(见 3.8)与 CLI 接入 + 任务持久化(见 3.9)均已完成，对应 PRD 的 **Task 模式**。CLI 默认 `AgentSession(orchestrate=True)`,一轮对话走 规划→按依赖执行→失败重规划;`RunEvent` 经 `_print_run_event` 渲染到 spinner、任务面板(含 blocked/failed 字形)与工具行;Ctrl-C 触发 `CancelToken` 协作式取消;TaskStore 快照落 SQLite `runs/tasks`,`/session switch` 与重启后可恢复任务状态。PRD 新增的 **Loop 模式**（GoalSpec/LoopRunner/Verifier/Learner）在此之上，单列 6.14。

**「只说不做」问题已修复**（commit `2caddfa`）：
- 强化 Planner system prompt：明确要求只输出 JSON，任务 content 必须包含具体工具调用，避免「确认」「检查」等空泛任务
- 强化 Executor 任务指令：明确要求「必须立即调用工具」，列举具体场景，禁止只输出文字
- 新增空转检测：第一轮不调用工具时给模型纠正提示，智能识别合法完成消息（「已完成」「无需操作」）避免误伤

接下来要做：

- **P0：抽出 Runtime Service**，统一提供 create/switch session、send message、cancel、subscribe events、approve/deny 和 close；CLI 先改为调用 Service，不能让 Web/TUI 直接依赖 `_build_session` 或复制 `_session_factory`。
- 在 Runtime Service 内新增异步 `ApprovalBroker`：Executor 发结构化 ApprovalRequest 后暂停当前 run，由 CLI/Web/TUI 提交 decision；保留同步 Policy adapter 兼容现有 CLI 和测试。
- Replanner 当前是启发式;后续可选"让模型看 outcome 产出 plan patch"的 LLM 重规划。
- `runs / tasks` 已落库,但还没有 CLI/Web 查看入口(如 `/runs`、任务历史回看)。
- 编排路径会把"子任务指令"作为 user 消息写进历史,多轮后上下文偏长。PRD §7.3 的上下文压缩(见 6.13)会在 token 接近窗口上限时自动压缩旧历史兜底,任务不会因超限中断;"用独立通道传子任务指令、根本不进对话历史"是可选的源头优化,优先级下调。

验收标准:CLI 跑一个复杂目标时,能看到计划生成、任务按依赖推进、失败自动补救、审批拒绝后阻塞,且 Ctrl-C 能干净取消、重启后任务状态可恢复。(已满足,见 3.8 / 3.9 与 `tests/unit/test_orchestrator.py`、`test_runtime.py`、`test_storage.py`、`test_session_router.py`。)

### 6.2 多 Agent、Session 与长期记忆

当前状态：核心功能已完成（见 3.4）。CLI prompt 显示
`[session_id·标题] ▸`，`/session list` 显示消息数；`read_write` 策略在
`SessionRouter.close_all()` 中生成会话摘要并写入长期记忆。

接下来要做（非阻塞优化）：

- `memories` 升级为向量检索（当前 LIKE 全文匹配，够 MVP；需新增依赖或复用 Ollama embedding，待评估方案）。

### 6.3 模型层

当前状态：三种 adapter 可用；本地模型可用但不支持 tools 的模型能力不完整。

接下来要做：

- 启动时统一校验 `chat / tools / streaming / json_action` 能力。
- 为不支持原生 tools 但 JSON 稳定的模型补 `json_action_adapter`，默认限制高风险工具。
- 建立 Ollama、LM Studio、vLLM 兼容矩阵，记录工具调用、流式、上下文长度限制。
- 把云端数据边界提示从 CLI banner 升级为 RunEvent 级别，让 Web UI 也能显示。

验收标准：同一 AgentProfile 能在支持 tools 的云端模型和本地模型间切换；不支持 tools 的模型不会被误放行执行危险工具。

### 6.4 工具与审批

当前状态：统一 `ToolDescriptor`、九级风险、参数级 `approval_resolver`、
结构化交互审批和 `ToolAuditEvent` 已完成（见 3.12）。workspace 越界和高风险
动作不可持久化授权；内置工具与 MCP 均声明风险/目标/来源元数据，并由 CLI
Session 统一写入 `tool_executions`。`web_search` / `web_fetch` 标为
`network`，当前通过显式只读策略免逐次审批。

接下来要做：

- 在 Runtime Service 的 Approval Broker 中实现结构化会话授权，授权键绑定
  `tool + risk + origin + host + scope`，不能只按 action 字符串记忆。
- 增加敏感动作分类器：删除、支付、发布、上传、授权、部署等动作提升为
  `destructive` 或二次确认，普通 browser/write 授权不能绕过。
- 增加审计查询与回放接口，按 session/run/tool/risk/outcome 过滤，并让 Loop
  evidence 引用具体 `tool_executions.id`。
- 为更多工具补专用 `audit_redactor`；参数和结果只存必要摘要，不落正文、凭据、
  cookie 或终端密码输入。

验收标准：同一审批策略可同时判断 `write_file`、`shell`、`browser_click`、MCP
tool 和 remote command；基础 Descriptor 与内置/MCP 审计已满足，browser/remote
待 ControlGateway 接入。

互联网搜索的 provider、抓取、来源、引用和缓存路线统一放在 6.15，本节只维护 ToolDescriptor、审批和审计的通用边界。

### 6.5 MCP

当前状态：stdio Client 可用，Playwright 已接入；Windows 的 npm shim
解析、必需环境变量、稳定 cwd 和 MCP SDK 进程树清理已接入，并有三平台
专项 CI。MCP 工具已映射为 ToolDescriptor，继承 Server risk/origin/host，
调用成功、协议错误和审批结果进入统一工具审计（见 3.3、3.12）。

接下来要做：

- 增加 Streamable HTTP transport。
- 支持在 Server 默认风险之上按工具配置提高风险等级，禁止工具自行降低风险。
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

当前状态：SQLite 已落 `sessions / messages / memories / tool_executions / runs / tasks / context_summaries`，以及 Loop 的 `goal_specs / loop_runs / loop_iterations / verification_results / worktrees / subagent_runs`（见 3.4 / 3.9 / 3.10 / 3.11）；尚无 `settings` 表，没有 Web UI，CLI 与未来 Web 尚未抽出共用 Runtime service。

接下来要做：

- 优先抽出 Runtime Service 与 Approval Broker，并让现有 CLI 通过该服务运行；这是 Server/TUI 的前置工作。
- 补 `settings` 表；把 LoopRunner 的运行过程和证据真正写入现有 `loop_store` 表。
- 新增 `app/server.py` 和 `app/web/`，先提供 health、sessions、messages、approvals、cancel、SSE 和 `agentlab serve`，默认只监听 `127.0.0.1`。
- API 稳定后再增加本地 Web UI、Loop Dashboard、证据/diff 页面和 Stop 按钮。
- 配置面板能查看 AgentProfile、模型 profile、Skill、MCP Server、Control Target 和工具风险等级。

验收标准：CLI 与 HTTP API 对同一 Session 使用同一 Runtime Service；`agentlab
serve --workspace .` 可发送消息、订阅事件、处理审批和取消 run，退出重启后仍能
恢复消息、任务、上下文摘要和工具审计。

### 6.11 安全、可观测性与测试

当前状态：已有 workspace 默认边界与越界动态审批、ToolDescriptor 九级风险、
结构化工具审计、工具级摘要脱敏、MCP env allowlist；测试以 unit 为主，MCP
已有 Windows/Linux/macOS 专项 CI，但完整 Server/浏览器/远程集成链路尚未建立。

接下来要做：

- API Key 从 `.env` 迁移到 macOS/Windows Keyring，`.env` 只做开发兜底。
- 把独立 approval decision、control action、model profile、actual model 补进审计事件，并与现有 ToolAuditEvent/run 串成可回放链路。
- 增加 provider fake、MCP test server、本地浏览器测试页、fake SSH target。
- 扩展三平台 CI，覆盖 editable install、CLI smoke、Runtime Service、FastAPI Server 和 Playwright 主链路。
- 高风险模块默认禁用，首次启用必须展示能力、数据边界和风险。

验收标准：一次包含模型推理、工具调用、审批、浏览器 observation 的 run 可被完整回放为事件和审计记录。

### 6.12 终端 TUI

当前状态：还没有 `app/tui/`；交互只有行式 CLI REPL（`app/cli.py`），没有全屏
分区界面，也没有欢迎栏。优先级后移到 Runtime Service 和 FastAPI Server 之后，
避免 TUI 复制 CLI 内部装配逻辑。

接下来要做：

- 新增 `app/tui/`（建议 Textual / Rich）：`app.py` 布局、`banner.py` 大号欢迎栏、`widgets.py` 组件、`events.py` 订阅 Runtime Event Bus。
- 顶部一个很大的欢迎栏，用 ASCII art 大字渲染 **Amaz1ng** 标题，附当前 Agent 和模型 profile；窄终端自动降级为单行标题。
- 分区布局：侧边栏（Agent/Session 列表）、Loop 面板（GoalSpec/iteration/验证结果/预算/worktree）、任务面板、上下文状态、对话区、内嵌审批对话框、状态栏（provider + 云端数据边界 + token/耗时）。
- 与 CLI / Web 共用同一 Runtime service，不复制 Agent 逻辑；`/session` 命令族在 TUI 同样可用。
- 新增启动入口 `python -m app tui`，并提供明显的 Stop / 取消入口对应紧急停止。

验收标准：`python -m app tui` 启动后显示 Amaz1ng 欢迎栏，可在 TUI 内切换 session、看到流式输出、任务面板和键盘可操作的审批。

### 6.13 上下文构建与压缩

当前状态：核心已完成(见 3.10)。`ContextBudget`(窗口解析 + token 估算 + 各项预算 + 70%/85% 阈值)、`ContextCompressor`(安全选段 + 结构化摘要 + 校验脱敏 + 就地替换)、`ContextManager`(稳定点 `maybe_compact` + context_* 事件 + 审计 flush)、`context_summaries` 表、`/context` 命令族、编排路径接入均已落地;无 context_manager 时行为完全不变。剩下的是非阻塞增强。

接下来要做(均为非阻塞增强):

- §7.3.1 的"完整稳定顺序上下文组装":目前 system prompt 仍由 `build_system_prompt` + Skill + Memory 在 CLI 侧拼成,摘要走"就地顶掉旧消息"而非独立的 Context Summary 段;后续可把 System / Active Task / Skill / Tool / Memory / **Context Summary** / Recent / Evidence 收进统一的 Context Builder。
- 切换到更小窗口模型 / session 恢复时主动重算预算并压缩(当前只在编排稳定点按阈值触发)。
- CompressionPolicy:敏感本地数据要求本地小模型做摘要,或云端模型参与前二次确认(当前统一用会话模型 + 脱敏)。
- `memory_candidates` → 经 MemoryPolicy 确认后写长期记忆的人工入口(当前只作为摘要候选字段,不落库)。
- TUI / Web 复用 `/context` 能力(report/compact/summary)。

验收标准:一个会超出模型窗口的长 session,在 85% 阈值自动压缩后仍能继续推进;压缩摘要保留 user_goal/decisions/open_tasks 且不丢未完成 run;`/context` 可查看预算与摘要,`/context compact` 可手动触发;压缩前后 token 数与摘要范围可审计。(已满足,见 3.10 与 `tests/unit/test_context.py`。)

### 6.14 Loop Engineering 模式（基础闭环已实现）

当前状态：**基础端到端闭环已完成**（见 3.11）。GoalSpec + Verifier (command/file) + LoopRunner + Orchestrator + WorktreeManager + loop_store 建表 + Loop RunEvent + CLI `/goal` `/loop` 已连接并通过测试。worktree 相对路径、未跟踪文件、审批提交与合并边界已经补齐。

PRD 模式分层（§7.6.1）：Prompt 模式（单轮）→ Task 模式（Planner/Executor/Replanner，已实现）→ Loop 模式（执行与验证基础闭环已实现，学习与协作能力待完善）。

接下来要做（优先级从高到低）：

- **证据持久化**：LoopRunner 每轮把 run / iteration / verification result / worktree / commit SHA 写入现有 `loop_store`，并实现 `/loop evidence`、`/loop diff`。
- **Verifier 扩展**：browser（Playwright MCP）、api（HTTP 请求）、human（人工确认）类型；flaky 重试逻辑；evidence_ref 链接到 tool_executions。
- **CLI 命令补全**：`/goal edit/verify` 与 `/loop evidence/diff/learn/clean`，修改成功标准和清理 dirty worktree 必须审批。
- `app/agent/learner.py`：仅在稳定点（成功/阻塞/预算耗尽/停止）生成 `memory_candidate / skill_update_proposal / project_knowledge_update / anti_pattern` 候选，不自动落库；密钥/cookie/私钥/验证码不写记忆。
- `app/workspace/knowledge.py`：Project Knowledge 索引（README/AGENTS/CLAUDE/SKILL/测试命令/历史 loop 经验），每条标来源；与实际代码冲突时以代码和验证结果为准。
- `app/agent/subagents.py`：子 Agent 协作（Executor/Verifier/Reviewer/Research），独立 session + TaskStore snapshot + 临时授权；不能降低 GoalSpec 或扩权；输出回父 LoopRunner 统一审计；验证 Agent 不复用执行 Agent 私有推理上下文。
- 定时/后台 Loop（`app/workspace/scheduler.py`）：手动 `/loop` / 本地 cron / GitHub Actions 三类入口；后台 loop 默认禁用、只跑预配置 GoalSpec、高风险动作转人工审批。优先级最低，可放最后。

安全要求（PRD §12）：Loop 默认要求至少一个非 `llm_judge` verifier；默认 `git_worktree`；默认禁用后台自动运行 / 自动 push / 发布 / 部署；模型提出降低 success_criteria 必须审批。

验收标准：CLI 用 `/goal new` + `/loop start` 跑一个带 `command` verifier 的代码目标，能看到 GoalSpec 校验、worktree 准备、规划→执行→验证→（失败则）诊断修复再验证、预算耗尽/验证通过/用户停止分别落到对应状态与 RunEvent，证据和 diff 可在 `/loop evidence` `/loop diff` 回看，且主工作区不被未验证改动污染。

### 6.15 受控互联网检索与引用

当前状态：已有可用的 `web_search` 和 `web_fetch`（见 3.2），能通过 DuckDuckGo/
HTML fallback 搜索公网，并在 SSRF、重定向和体积限制下抓取静态 HTML
正文；两者已是 `network` ToolDescriptor 并进入工具审计。但实现仍直接绑在
tool 内，没有统一 provider 协议、source/document id、正文缓存、引用校验、
`web_find`、PDF 提取或受控浏览器升级。

接下来要做（按依赖顺序）：

1. **检索核心骨架**：新增 `app/retrieval/models.py` 与 `service.py`，定义
   SearchRequest/Result、FetchedDocument、Citation 和 WebRetrievalService；现有
   `web_search/web_fetch` 改为薄适配器，保持旧 tool schema 兼容。
2. **Provider 与来源链**：抽出 SearchProvider 协议，先用现有 DuckDuckGo
   实现 adapter，增加 fake provider；新增 retrieval_sources/documents/citations
   存储与 CitationManager，让回答可回溯 URL、content hash 和 fetched_at。
3. **文档与缓存**：新增 `web_find`、normalized URL/content hash/TTL 缓存、
   ETag/Last-Modified、HTML/text/PDF 提取和 ContextBudget 片段选择；大文本放
   blob/cache，不自动写长期记忆。
4. **安全与动态降级**：在当前 SSRF 防护上补实际连接 IP 校验、DNS
   rebinding、解压体积、MIME 和 prompt-injection fixture；静态抓取失败时只能
   经 ControlGateway + 新 `browser_control` 审批升级，不携带日常浏览器 cookie。
5. **事件、API 与三平台验收**：增加 retrieval_* RunEvent、来源/文档/缓存
   API，并在 Windows/Linux/macOS CI 用 fake provider + 本地 HTTP 测试站覆盖搜索、
   抓取、引用、取消、provider 失败和外部页面不能扩权。

验收标准：模型只产生结构化检索 tool call，不直接持有网络或 provider
密钥；Agent 能针对最新信息搜索并抓取原始来源，最终回答使用 Runtime
校验过的可点击引用；SSRF、缓存时效、provider 边界、prompt injection 和
浏览器升级均可测试、可取消、可审计。完整目标见 PRD §7.12。

---

## 7. MCP 接入路线

原则：内置 `read_file / write_file / list_dir / code_search / shell` 是基础能力，不被 MCP 替代。MCP 主要用于连接外部系统、专业工具和可选增强能力。若 MCP 提供同类能力，应作为增强 backend 或用户显式选择的工具。

| 优先级 | MCP 类型 | 当前状态 | 下一步 |
|---|---|---|---|
| P0 | Playwright MCP | 已接入跨平台 stdio；Windows `npx.cmd`、运行环境和三平台专项 CI 已完成 | 纳入 ControlGateway，补 origin 级审批和审计 |
| P1 | Git MCP | 未接入 | 先做只读 status/diff/log/branch；checkout/commit/reset 必须审批 |
| P1 | GitHub MCP | 未接入 | issue/PR 读取先行；评论、改 PR、触发 workflow 前审批；token 不入日志 |
| P1 | IDE/LSP MCP | 未接入 | diagnostics、definition、references、symbol search，用于增强 `code_search` |
| P2 | Database MCP | 未接入 | 默认只读 schema/query；写 SQL/DDL 默认禁用或强审批 |
| P2 | Remote Host MCP | 未接入 | 与 SSH Runner 互补，只允许预配置 host 和 remote workspace |
| P2 | Docs/Search MCP | 未接入 | 内部文档、API 文档、知识库检索；作为 SearchProvider/connector 接入 6.15 的 source/citation 链，不另建结果格式 |
| P3 | Cloud/DevOps MCP | 未接入 | 只读观察先接入；部署、扩缩容、删除资源必须二次确认 |

---

## 8. 运行与验证

推荐环境是 conda 环境 `agentlab`，Python 3.11（MCP SDK 要求 ≥3.10）。

```bash
conda activate agentlab

# 首次安装，注册可从任意目录调用的 agentlab 命令。
python -m pip install -e .

# 收集测试。当前数量以 3.7 和 pytest 输出为准。
python -m pytest tests/unit --collect-only -q

# 运行全部 unit tests。
python -m pytest tests/unit -q

# 从任意目录进入交互模式，当前目录作为 Agent workspace。
agentlab --workspace .

# 单次 prompt。
agentlab --workspace . --profile cloud_claude -p "list_dir 看下当前目录"

# 本地模型。需要先启动 Ollama 并下载模型。
agentlab --workspace . --profile local_qwen -p "list_dir 看下当前目录"

# 源码目录内仍可使用开发入口。
python -m app
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
# macOS / Linux
cp config/mcp_servers.example.yaml config/mcp_servers.yaml
# 编辑 config/mcp_servers.yaml，把 playwright.enabled 改成 true。
python -m app --profile cloud_claude -p "打开 https://example.com 并告诉我页面标题"
```

```powershell
# Windows PowerShell
Copy-Item config\mcp_servers.example.yaml config\mcp_servers.yaml
# 编辑 config\mcp_servers.yaml，把 playwright.enabled 改成 true。
python -m app --profile cloud_claude -p "打开 https://example.com 并告诉我页面标题"
```

注意：

- Playwright MCP 首次启动可能通过 `npx` 下载 server 和浏览器内核。
- Windows 自动将 `npx` 解析为 `npx.cmd`；若启动失败，先检查
  `node --version` 与 `npx --version`。
- named persistent profile 的登录态保存在 `data/browser-profiles/<name>`，不入库。
- 云端模型配合浏览器控制时，页面 DOM、截图摘要、表单内容可能进入云端模型上下文。

---

## 9. 接手注意事项

- 先读 PRD 的目标设计，再读本文件判断当前代码缺口。
- 做实现优先保持现有模式：Python dataclass、pytest unit test、fake provider/fake manager、workspace 默认边界与越界审批、脱敏。
- 查代码优先用 `rg` 或内置 `code_search`，不要让模型通过 shell 拼复杂 grep/find。
- 修改 PRD 时只改目标设计；修改进度、完成情况、下一步计划时只改本文件，**完成项归到第 3 节而不是在下一步表里标记**。
- 高风险能力的顺序应是：先结构化描述和审计，再接真实执行能力。
