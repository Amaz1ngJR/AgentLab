# AgentLab 进展与交接文档

本文档用于让新的 AI 或开发者快速接手项目：先看当前进展，再按 PRD 继续推进。总体目标和最终设计只维护在 [`technical_architecture.md`](./technical_architecture.md)，不要把阶段进展、更新时间或临时代码状态写进 PRD。

维护规则：

- `technical_architecture.md` 是目标方案和 PRD，只描述“最终应该是什么”。
- `process.md` 是当前进展和执行计划，必须始终写清楚“现在做到哪里、还缺什么、下一步先做什么”。
- **完成的工作统一归到第 3 节「已完成里程碑」**，不要在各模块的"下一步"表里用删除线或"已完成"标记堆积；模块章节只保留真正待做的事。
- 「当前需要做的事情」最多保留 5 项，避免变成无限待办清单。

---

## 1. 当前一句话状态

AgentLab 是一个可运行的本地 CLI Agent：支持模型 profile 切换、云端/本地 adapter、流式输出、多轮工具调用、方向键审批、内置文件/代码搜索/web 搜索/shell/交互式终端/todo 工具、stdio MCP Client（Playwright 浏览器控制）、多 Agent `/session` 切换、SQLite 持久化与长期记忆、Skill Loader（按 AgentProfile 注入工作流上下文）、`Planner + Executor + Replanner` 编排路径（带依赖 TaskStore + 结构化 `RunEvent`，已接入 CLI 主路径，支持 Ctrl-C 协作式取消与任务状态持久化恢复）、上下文预算与自动压缩（`ContextBudget` + `ContextCompressor` + 结构化摘要 + `/context` 命令族，编排稳定点超阈值自动压缩、可审计）。

**Loop 模式（Loop Engineering，§7.6）的基础闭环已接通**：`/goal new` 定义目标和验证命令，`/loop start` 创建隔离 worktree，调用 `Orchestrator` 执行 Planner→Executor→Replanner，随后进入 Verifier 验证；失败时生成修复指令继续迭代，成功时展示完整 worktree 状态，并在用户审批后生成可合并提交。command Verifier 复用同一审批策略和跨平台 shell 解释器；执行器异常会直接终止 Loop，不能再被旧文件或旧测试误判为成功。尚缺 browser/api/human Verifier、Loop 运行证据完整持久化、`Learner` / Project Knowledge、子 Agent 和后台 Loop。

距离 PRD 的核心缺口：Loop Engineering 的高级验证、学习与协作能力尚未完成；没有统一风险等级 `ToolDescriptor` 与分级审批，没有自建 Computer Control Gateway，没有终端 TUI，没有 Web UI。

---

## 2. 当前需要做的事情（最多 5 项）

1. **完善 Loop Engineering 证据闭环**（PRD §7.6，基础闭环见 3.11）：把 loop run / iteration / verification / worktree / commit 证据完整写入 `loop_store`，并实现 browser/api/human Verifier 与 `/loop evidence`、`/loop diff`。
2. 把 `Tool` 升级为统一 `ToolDescriptor`，审批从布尔值改为分级策略（read/write/execute/browser_control/...）（见 6.4）。
3. 建立 `ComputerControlGateway`，把 Playwright MCP 浏览器能力纳入统一观察、动作、审批、审计链路（见 6.6）。
4. 新增终端 TUI（`app/tui/`）：顶部大号 **Amaz1ng** 欢迎栏 + 会话/任务/审批/对话分区，复用同一 Runtime 事件（见 6.12）。
5. FastAPI Web UI + SSE 事件，与 CLI 共用同一 Runtime service（见 6.10）。

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

- **488 个 unit tests**（其中 6 个在可选依赖未装时 skip，全离线），覆盖：runtime（含编排委托 + 取消）、Orchestrator/Planner/Executor/Replanner 编排路径、TaskStore（依赖/claim/状态回写/snapshot/restore）、上下文预算与压缩（token 估算/预算阈值/安全选段/摘要校验脱敏/ContextManager/storage）、三种 adapter、MCP（config/adapter/manager，含 Windows `npx.cmd`、最小运行环境和 cwd）、code_search、web_search、web_fetch（公网地址校验、DNS/重定向 SSRF、正文抽取、截断、脱敏）、shell、交互式终端会话、审批、workspace path、存储、记忆、session_router、CLI、Skill loader/catalog、Loop Engineering（真实多轮编排、Verifier 审批、worktree 相对路径/未跟踪文件/审批提交与合并边界、执行异常终止）。
- `.github/workflows/mcp-cross-platform.yml` 在 Windows、Linux、macOS
  runner 安装 Node.js 后真实验证 `npx` 解析，并运行 MCP 专项测试。

### 3.8 Planner / Executor / Replanner 编排与结构化 RunEvent

- `app/agent/tasks.py`：`TaskStore` 升级为任务状态唯一来源。`Task` 新增 `dependencies / evidence / error / history`；状态增 `blocked / failed`（编排路径专用,`todo_write` 仍只写简单三态）。新增 `add / extend / get / update_status / claim_next（按依赖）/ has_runnable / has_open / is_done / is_stalled / snapshot`。`summary()` 向后兼容(保留 total/pending/in_progress/completed,加 blocked/failed)。
- `app/agent/events.py`：结构化 `RunEvent`，kind 覆盖 `run_started / plan_created / task_started / message_delta / tool_requested / approval_required / tool_completed / tool_denied / task_updated / run_completed / run_failed`;`payload.tasks` 携带任务 snapshot。
- `app/agent/planner.py`：`Planner` 让模型只输出 JSON 计划,`_extract_json` 容忍 markdown 围栏/前后散文,`_parse_tasks` 跳过非法项并去重 id;解析失败或模型异常时退化为单任务计划,保证永不卡死。
- `app/agent/executor.py`：`Executor.run_task` 按"单个子任务"驱动有限步工具循环,把任务指令注入共享 messages,产出 `TaskOutcome(completed/failed/blocked)`;工具出错→failed,审批被拒→blocked,步数耗尽→failed;发 `RunEvent`,不直接写 TaskStore。
- `app/agent/replanner.py`：`Replanner.apply` 把 `TaskOutcome` 回写 TaskStore;失败任务追加一次"复查并修复"补救任务(重试任务再失败不再追加,避免无限循环),阻塞不追加。
- `app/agent/orchestrator.py`：`Orchestrator.run(goal)` 串联 规划→claim→执行→重规划,带全局 `max_steps` 预算与协作式取消(`app/agent/cancel.py` 的 `CancelToken`);多次 `run()` 在已有任务之上追加新计划(任务 id 加 `rN-` 前缀避免跨 run 撞车),实现"用户中途追加目标";收工/卡死/取消/超预算分别发对应 RunEvent。
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

---

## 4. 当前能力快照

| 模块 | 当前进展 | 关键文件 |
|---|---|---|
| CLI | 交互 REPL、`-p`、`--profile`、`-y`、`/reset`、`/session` 命令族 + 斜杠补全；spinner、任务面板、统计 | `app/cli.py` |
| 配置 | `.env` + `config/models.yaml`（模型）+ `config/agents.yaml`（Agent）+ `config/mcp_servers.yaml`（MCP） | `app/config/`, `app/agent/profiles.py`, `app/mcp/config.py` |
| 模型层 | Anthropic / OpenAI Responses / OpenAI-compatible；统一内部协议；JSON tool call fallback | `app/models/` |
| Runtime | CLI 主路径已切到编排:`AgentSession(orchestrate=True)` 委托 Orchestrator;`orchestrate=False` 仍保留 legacy 单轮循环 | `app/agent/runtime.py`, `app/agent/orchestrator.py` |
| 编排 | Planner(JSON 计划) + Executor(单任务工具循环,带 progress/流式/用量) + Replanner(失败追加补救) + CancelToken + 结构化 RunEvent;已接 CLI（PRD 的 Task 模式） | `app/agent/planner.py`, `app/agent/executor.py`, `app/agent/replanner.py`, `app/agent/events.py`, `app/agent/cancel.py` |
| Loop Engineering | **基础闭环已实现**：GoalSpec → Orchestrator 执行 → Verifier → 诊断修复 → worktree 审批提交；待完善：扩展 Verifier、完整证据持久化、Learner、Project Knowledge、子 Agent（见 6.14） | `app/agent/goals.py`, `verifier.py`, `loop_runner.py`, `app/workspace/worktree.py`, `app/storage/loop_store.py` |
| 多 Agent / Session | AgentProfile + SessionRouter + `/session` 命令族；SQLite 持久化、恢复、隔离（含任务快照恢复） | `app/agent/profiles.py`, `app/agent/session_router.py`, `app/storage/` |
| 长期记忆 | none/read/read_write 三策略 + 注入；LIKE 检索（未做向量） | `app/memory/` |
| Skill | Loader + Catalog：扫 `skills/*/SKILL.md`、按 AgentProfile 注入工作流；只影响上下文不授权 | `app/skills/`, `skills/` |
| 任务面板 / TaskStore | 任务状态唯一源:依赖/claim/blocked/failed/evidence/history/snapshot/restore;`todo_write` 走简单三态;CLI 面板渲染(含 blocked/failed 字形) | `app/agent/tasks.py`, `app/tools/builtin/todo.py` |
| 审批 | 自动 / 交互（方向键）/ 拒绝；仍是 `requires_approval` 布尔模型 | `app/agent/approval.py`, `app/util/menu.py` |
| 内置工具 | `read_file / write_file / edit_file / list_dir / code_search / web_search / web_fetch / shell / todo_write`；`terminal_*` 交互式终端会话 | `app/tools/builtin/` |
| MCP | stdio Manager、工具发现、sync/async 桥、同名不覆盖、auto_approve 白名单 | `app/mcp/` |
| 浏览器控制 | Playwright MCP：打开/snapshot/点击/输入；named profile；数据边界提示 | `config/mcp_servers.example.yaml`, `app/cli.py` |
| 存储 | SQLite：sessions/messages/memories/tool_executions/runs/tasks；settings 表与 Web 复用待做 | `app/storage/` |
| 安全基础 | workspace 越界拒绝、脱敏、MCP env allowlist、敏感目录不入库 | `app/util/redact.py`, `.gitignore` |
| 取消 | Ctrl-C 协作式取消(CancelToken):首次置位、当前步骤后停止,连按强制中断 | `app/agent/cancel.py`, `app/cli.py` |
| 上下文压缩 | ContextBudget + ContextCompressor + 结构化摘要;编排稳定点超 85% 自动压缩、可审计;`/context` 命令族 | `app/agent/context.py`, `context_budget.py`, `context_compaction.py` |
| 测试 | 483 个 unit tests（web_search 的 6 个在可选依赖未装时自动 skip） | `tests/unit/` |

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
      loop_commands.py             # [Loop] LoopCommandHandler:/goal /loop 命令处理(待接 CLI)
    workspace/                     # [Loop] 工作区隔离与项目知识
      worktree.py                  # WorktreeManager:git worktree 创建/diff/dirty/删除/合并建议
    tools/
      registry.py                  # Tool 注册表（仍是 requires_approval 布尔模型）
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
  tests/unit/                      # 444 个 unit tests
```

尚未出现但 PRD 已规划的目录/模块：`app/agent/learner.py`、`app/agent/subagents.py`、`app/workspace/knowledge.py`、`app/workspace/scheduler.py`、`app/control/`、`app/tui/`、`app/server.py`、`app/web/`。（Loop 核心 `goals.py` / `verifier.py` / `loop_runner.py` / `loop_store.py` / `app/workspace/worktree.py` 已落地，见 3.11。）

---

## 6. 模块进展与下一步

> 已完成的能力见第 3 节。本节各模块只列"当前状态 + 还要做什么 + 验收标准"。

### 6.1 Runtime 与任务拆解

当前状态：编排核心(见 3.8)与 CLI 接入 + 任务持久化(见 3.9)均已完成，对应 PRD 的 **Task 模式**。CLI 默认 `AgentSession(orchestrate=True)`,一轮对话走 规划→按依赖执行→失败重规划;`RunEvent` 经 `_print_run_event` 渲染到 spinner、任务面板(含 blocked/failed 字形)与工具行;Ctrl-C 触发 `CancelToken` 协作式取消;TaskStore 快照落 SQLite `runs/tasks`,`/session switch` 与重启后可恢复任务状态。PRD 新增的 **Loop 模式**（GoalSpec/LoopRunner/Verifier/Learner）在此之上，单列 6.14。

**「只说不做」问题已修复**（commit `2caddfa`）：
- 强化 Planner system prompt：明确要求只输出 JSON，任务 content 必须包含具体工具调用，避免「确认」「检查」等空泛任务
- 强化 Executor 任务指令：明确要求「必须立即调用工具」，列举具体场景，禁止只输出文字
- 新增空转检测：第一轮不调用工具时给模型纠正提示，智能识别合法完成消息（「已完成」「无需操作」）避免误伤

接下来要做(均为非阻塞增强):

- `approval_required` 目前靠 Executor 内同步调 `ApprovalPolicy`(方向键菜单已生效);后续可把审批也做成异步 RunEvent,便于 Web UI/TUI 统一弹窗。
- Replanner 当前是启发式;后续可选"让模型看 outcome 产出 plan patch"的 LLM 重规划。
- `runs / tasks` 已落库,但还没有 CLI/Web 查看入口(如 `/runs`、任务历史回看)。
- 编排路径会把"子任务指令"作为 user 消息写进历史,多轮后上下文偏长。PRD §7.3 的上下文压缩(见 6.13)会在 token 接近窗口上限时自动压缩旧历史兜底,任务不会因超限中断;"用独立通道传子任务指令、根本不进对话历史"是可选的源头优化,优先级下调。

验收标准:CLI 跑一个复杂目标时,能看到计划生成、任务按依赖推进、失败自动补救、审批拒绝后阻塞,且 Ctrl-C 能干净取消、重启后任务状态可恢复。(已满足,见 3.8 / 3.9 与 `tests/unit/test_orchestrator.py`、`test_runtime.py`、`test_storage.py`、`test_session_router.py`。)

### 6.2 多 Agent、Session 与长期记忆

当前状态：核心功能已完成（见 3.4）。**CLI prompt 显示 session_id·标题** 和 **read_write 退出写摘要** 已实现（见下方）。

已完成（本次提交）：
- ✅ CLI prompt 动态显示 `[session_id·标题] ▸`（app/cli.py，从 storage 读取 session title，标题超过 30 字符时截断）
- ✅ `read_write` 记忆策略的"会话结束写摘要"接入 CLI 退出钩子：`SessionRouter.close_all()` 调用 `mem_policy.save()`（app/agent/session_router.py）
- ✅ `/session list` 显示每个会话的消息数（已完成，commit `948774f`）

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

当前状态：`Tool` 只有 `requires_approval` 布尔字段；内置工具齐全（含 `web_search` 和 `web_fetch`，见 3.2）；MCP 工具走 auto_approve 白名单。

**web_fetch 已实现**：
- 给定 URL 抓取网页正文并转 Markdown（requests + trafilatura/readability/BeautifulSoup）
- 比浏览器 DOM 点击更轻量，适合「读一篇文章」场景
- SSRF 防护：只允许公网 http/https，解析全部 DNS 地址，拒绝本机/私网/链路本地/保留地址；关闭自动重定向并逐跳校验
- 依赖未装时优雅降级（BeautifulSoup 纯文本兜底）
- 30 个单元测试全通过
- 解决「读知乎文章失败」等 web_search 只给摘要的缺口

接下来要做：

- 将 `Tool` 升级为 `ToolDescriptor`，补 `risk / target_type / scope / origin / host / requires_observation / audit_redactor`。`web_search` 属 `network` 只读风险，应在此分级里明确标注。
- 审批升级为分级策略：`read / observe / network / write / browser_control / desktop_control / remote_execute / execute / destructive`。
- 支持会话级授权（绑定 tool/origin/host/workspace）；删除、支付、发布、上传等动作不能被普通授权绕过。
- 内置工具、MCP 工具、浏览器动作、远程动作统一进入审计摘要。

验收标准：同一审批策略可同时判断 `write_file`、`shell`、`browser_click`、MCP tool 和 remote command。

**web_search 相关增强（非阻塞）**：

- `duckduckgo-search` 库偶发限流/验证码，当前已有 HTML fallback；后续可补 Bing/Google（需 API key）作为可选后端，用 profile 或 env 选择。
- 把搜索查询与结果 URL 纳入 `tool_executions` 审计（当前只脱敏不落审计）。

### 6.5 MCP

当前状态：stdio Client 可用，Playwright 已接入；Windows 的 npm shim
解析、必需环境变量、稳定 cwd 和 MCP SDK 进程树清理已接入，并有三平台
专项 CI（见 3.3）。

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

当前状态：SQLite 已落 `sessions / messages / memories / tool_executions / runs / tasks / context_summaries`，以及 Loop 的 `goal_specs / loop_runs / loop_iterations / verification_results / worktrees / subagent_runs`（见 3.4 / 3.9 / 3.10 / 3.11）；尚无 `settings` 表，没有 Web UI，CLI 与未来 Web 尚未抽出共用 Runtime service。

接下来要做：

- 补 `settings` 表；把 LoopRunner 的运行过程和证据真正写入现有 `loop_store` 表。
- 抽出 Runtime service，让 CLI 与 Web UI 共用同一逻辑。
- 新增 `app/server.py` 和 `app/web/`，提供本地 Web UI、SSE 事件、审批 API、Loop Dashboard / Loop API、Stop 按钮。
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

### 6.12 终端 TUI

当前状态：还没有 `app/tui/`；交互只有行式 CLI REPL（`app/cli.py`），没有全屏分区界面，也没有欢迎栏。

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
| P2 | Docs/Search MCP | 未接入 | 内部文档、API 文档、知识库检索；结果必须标注来源 |
| P3 | Cloud/DevOps MCP | 未接入 | 只读观察先接入；部署、扩缩容、删除资源必须二次确认 |

---

## 8. 运行与验证

推荐环境是 conda 环境 `agentlab`，Python 3.11（MCP SDK 要求 ≥3.10）。

```bash
conda activate agentlab

# 收集测试。当前 488 个 unit tests。
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
- 做实现优先保持现有模式：Python dataclass、pytest unit test、fake provider/fake manager、workspace 限制、脱敏。
- 查代码优先用 `rg` 或内置 `code_search`，不要让模型通过 shell 拼复杂 grep/find。
- 修改 PRD 时只改目标设计；修改进度、完成情况、下一步计划时只改本文件，**完成项归到第 3 节而不是在下一步表里标记**。
- 高风险能力的顺序应是：先结构化描述和审计，再接真实执行能力。
