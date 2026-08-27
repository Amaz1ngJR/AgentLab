# AgentLab 借鉴 OpenAI Codex Runtime 的演进方案

| 项目 | 内容 |
|---|---|
| 文档定位 | Codex Runtime 架构调研、AgentLab 差距分析与实施路线 |
| 调研对象 | [openai/codex](https://github.com/openai/codex) 的 `codex-core`、`codex-protocol`、`app-server`、沙箱与上下文压缩实现 |
| 目标 | 让 AgentLab 在多前端复用、执行效率、上下文质量、恢复能力和系统安全方面持续演进 |
| 约束 | 不照搬 Rust 实现；保持 Python 3.11、跨 macOS/Windows/Linux、本地优先和现有 CLI 兼容 |

---

## 1. 结论摘要

AgentLab 当前已经具备 `RuntimeService`、`ApprovalBroker`、`AgentSession`、
`Orchestrator`、`RunEvent`、SQLite 持久化和上下文压缩，整体方向与 Codex Runtime
一致。主要差距不是缺少某个工具，而是 Runtime 的边界仍不够稳定：

1. `RuntimeService.send_message()` 仍是同步阻塞调用，Web/TUI 难以直接复用。
2. Session、Run、Message、Task、Tool Audit 分散存储，缺少统一的 Turn/Item 事实流。
3. `RuntimeEvent.payload` 为 `Any`，不能作为稳定、可版本化的公共协议。
4. CLI 仍直接访问 Router、Session 私有字段和装配细节。
5. Legacy 工具循环与 Orchestrator 并存，审批、取消、流式和错误处理存在双份实现。
6. 所有编排请求默认先调用 Planner，简单任务会额外增加延迟和 token。
7. 工具 Schema 通常全量发送，MCP/浏览器/远程工具增多后固定上下文会膨胀。
8. Shell 只有应用层路径约束与审批，没有操作系统级沙箱。

建议优先完成三件事：

- 统一 `Thread / Turn / Item` 数据模型与 `TurnEngine`。
- 建立版本化 Protocol 和异步 Submission/Event Queue。
- 建立 `ExecutionGateway` 与跨平台系统沙箱。

---

## 2. Codex Runtime 值得借鉴的关键设计

### 2.1 Core 不直接输出 UI

Codex Core 禁止业务库直接写 stdout/stderr，所有可见信息必须经过 TUI、App Server
或 tracing 抽象。这保证 Core 不依赖任何具体前端。

AgentLab 应落实同样边界：

```text
CLI / TUI / Web / IDE
          ↓ Protocol
RuntimeService / TurnEngine
          ↓
Model / Tool / Approval / Context / Execution
```

目标规则：

- `app/agent/` 不 import `app.cli`，不调用 `print()` 或 prompt_toolkit。
- CLI 只提交操作、订阅事件和回答审批，不创建模型、工具或 MCP 生命周期。
- FastAPI、TUI 和未来 IDE 不复制 `_build_session` / `_session_factory`。
- Runtime 通过结构化事件表达所有用户可见状态。

### 2.2 SQ / EQ 异步通信

Codex Protocol 使用 Submission Queue / Event Queue：客户端提交操作，Runtime
异步发出事件。客户端不需要同步持有一次 `chat()` 调用。

AgentLab 目标接口：

```python
turn_id = runtime.start_turn(thread_id, input)
runtime.steer_turn(turn_id, input)
runtime.interrupt_turn(turn_id)
runtime.resume_turn(turn_id)
runtime.answer_request(request_id, response)

for event in runtime.subscribe(thread_id, after_seq=100):
    render(event)
```

CLI 可以在内部等待 `turn.completed`，但 Web/TUI 可直接异步消费事件。

### 2.3 Thread / Turn / Item

Codex App Server 的三个核心原语：

- **Thread**：长期对话，映射 AgentLab Session。
- **Turn**：一次用户请求到完成、失败、暂停或取消，映射 AgentLab Run。
- **Item**：Turn 内的用户输入、模型输出、推理、工具调用、审批、工具结果、文件修改。

建议数据结构：

```python
@dataclass(frozen=True)
class TurnItem:
    item_id: str
    thread_id: str
    turn_id: str
    sequence: int
    kind: str
    status: str
    created_at: str
    payload: dict[str, JsonValue]
```

采用 append-only Item 流后：

- 审批请求和工具结果可稳定关联。
- 进程崩溃后可确定 Turn 停在哪一步。
- Web/TUI 可统一回放历史。
- Session Fork、审计、证据和子 Agent 更容易实现。
- 不再需要频繁覆盖整份消息、任务和 Run 状态。

### 2.4 稳定的版本化 Protocol

Codex 的 Protocol crate 只定义数据类型，业务逻辑位于 Core。App Server 还能按当前
版本生成 TypeScript 和 JSON Schema。

AgentLab 可新增：

```text
app/protocol/
  envelopes.py
  commands.py
  events.py
  items.py
  errors.py
  schema.py
```

统一事件信封：

```python
@dataclass(frozen=True)
class EventEnvelope:
    schema_version: int
    sequence: int
    thread_id: str
    turn_id: str | None
    item_id: str | None
    kind: str
    timestamp: str
    payload: dict[str, JsonValue]
```

协议要求：

- 禁止 `Any`、Python 对象和异常实例进入公共 payload。
- 所有 ID 稳定，时间为 UTC ISO-8601。
- Event kind 使用命名空间，如 `turn.started`、`tool.completed`。
- 未知字段向前兼容，破坏性变更升级 `schema_version`。
- 可生成 OpenAPI / JSON Schema / TypeScript 类型。

### 2.5 Turn Steering、Suspend 与 RequestUserInput

Codex 明确定义 Steering、Suspend、RecoverTurn 和 RequestUserInput。AgentLab 应将
“修改建议”从普通拒绝提升为 Turn 状态：

```text
running
  → waiting_approval
  → waiting_user_input
  → running
  → completed / failed / cancelled
```

建议命令：

```python
runtime.suspend_turn(turn_id, reason="user_amendment")
runtime.submit_turn_input(turn_id, text, mode="steer")
runtime.recover_turn(turn_id)
```

收益：用户修改方向时保留当前 Task、预算和工具上下文，不必重新 Planner 整个任务。

### 2.6 Thread Settings Snapshot

Codex 每个 Thread/Turn 保存模型、推理强度、审批策略、权限、工作目录、沙箱、环境和
协作模式快照。

AgentLab 应在 Turn 开始时冻结：

```python
@dataclass(frozen=True)
class TurnContext:
    model_profile: str
    actual_model: str | None
    workspace: str
    tool_set_version: str
    approval_policy: dict
    permission_profile: dict
    context_policy: dict
    budgets: dict
    environment: dict
```

执行过程中不再读取可变化的 `SessionRouter.current_id`、全局 `.env` 或动态工具表。
这样模型切换、Session 切换和 Loop worktree 不会影响已经开始的 Turn。

### 2.7 Thread Fork 与 Rollout

Codex 支持 Thread Fork、Rollout Recorder 和历史截断。AgentLab 可增加：

```text
/session fork [turn_id]
```

应用：

- 从同一历史节点尝试另一种修复方案。
- 子 Agent 获得只读上下文分支。
- 两个 Worktree 并行实现，Verifier 选择结果。
- 保留原 Session，不让失败尝试持续污染上下文。

---

## 3. 提升效率和智能的 Runtime 改进

### 3.1 Direct / Task / Loop 自适应路由

当前 `orchestrate: true` 时每个请求先调用 Planner。简单聊天、解释和单工具操作会
多付一次模型延迟和 token。

目标模式：

| 模式 | 适用场景 | 是否 Planner |
|---|---|---|
| Direct | 问答、解释、单文件读取、单工具操作 | 否 |
| Task | 多文件、多步骤、有依赖任务 | 是 |
| Loop | GoalSpec + 验证 + 反复修复 | 是，且有 Verifier |

先用本地规则分类，避免额外分类模型调用：

```python
mode = ModeRouter.select(user_input, attachments, session_state)
```

升级规则：

- 默认 Direct。
- 明显包含多个修改、测试和验收动作时进入 Task。
- `/goal` 或显式 success criteria 进入 Loop。
- Direct 执行中发现复杂依赖时可升级 Task。
- Planner 失败时回退 Direct，而不是生成伪计划。

### 3.2 动态工具暴露

随着 MCP 和电脑控制工具增加，全量发送 Tool Schema 会浪费输入 token、降低模型工具
选择准确率并扩大攻击面。

建议始终暴露少量核心工具：

```text
read_file / code_search / edit_file / shell / todo_write / tool_search
```

高成本工具按需加载：

```text
browser_* / desktop_* / remote_* / cloud MCP / 特定数据库
```

模型通过 `tool_search` 请求能力，Runtime 根据 Profile、权限和任务动态注入。

验收指标：

- 普通 Coding Turn 的 Tool Schema token 减少至少 50%。
- 未授权工具不会出现在模型上下文。
- 小模型工具误选率不高于当前实现。

### 3.3 统一 TurnEngine

当前存在：

- `AgentSession._chat_legacy()`
- `Orchestrator → Executor`
- `LoopRunner → Orchestrator → Verifier`

应统一为一个 TurnEngine，模式只改变策略：

```text
TurnEngine
├── DirectPolicy
├── TaskPolicy
└── LoopPolicy
```

共享：

- 模型请求和重试
- 工具执行
- 审批和用户输入
- 取消和暂停
- token/耗时统计
- 上下文构建
- Event/Item 持久化

可消除审批、工具配对、图片、流式输出和 Provider 错误修两遍的问题。

### 3.4 Provider 重试、退避与熔断

AgentLab 已遇到 `503 system_cpu_overloaded`。应在模型请求层增加结构化策略：

```text
429 / 502 / 503 / 504 / 网络断开
→ Retry-After 优先
→ 指数退避 2s / 5s / 10s + jitter
→ 每次重试前检查取消
→ 发 provider.retrying 事件
→ 到上限后结构化失败
```

只重试尚未产生副作用的模型请求，不自动重放已执行写工具。

连续失败可触发短期熔断：

```text
连续 3 次过载 → 30 秒快速失败 → 提示降低 reasoning 或切换 Provider
```

### 3.5 结构化错误分类

不要让 Replanner 解析错误字符串。统一：

```python
@dataclass(frozen=True)
class RuntimeFailure:
    code: str
    category: str
    retryable: bool
    user_action_required: bool
    retry_after_seconds: float | None
    details: dict[str, JsonValue]
```

至少覆盖：

- `provider_overloaded`
- `rate_limited`
- `network_error`
- `context_overflow`
- `tool_failed`
- `approval_denied`
- `user_input_required`
- `sandbox_denied`
- `environment_missing`
- `budget_exhausted`
- `cancelled`

对应决策：

- Provider 过载：退避重试。
- 测试失败：Replanner 修复。
- 用户输入：Suspend。
- 权限拒绝：不重试。
- 上下文溢出：Compact 后重试。

### 3.6 Prompt Cache 与 Provider Continuation

Adapter 应声明能力：

```python
ProviderCapabilities(
    prompt_cache=True,
    continuation_id=True,
    streaming_usage=True,
    vision=True,
)
```

利用：

- Anthropic：稳定 System/Tool Block Prompt Caching。
- OpenAI Responses：服务端 continuation / response context。
- 本地模型：完整历史回放。
- Provider 切换：统一 Item 历史重建输入。

### 3.7 启动预热

Codex 有 Session Startup Prewarm。AgentLab 可并行预热：

- SQLite schema 与最近 Session metadata。
- 模型 endpoint 健康检查。
- MCP 工具清单缓存。
- Skill/Project Knowledge hash。
- 常用 Tool Schema 序列化。

目标：启动后的第一次有效 token 延迟不被配置扫描和 MCP 冷启动叠加。

---

## 4. 上下文质量演进

### 4.1 ContextBundle 与 World State

当前 AgentLab 主要将旧消息压成一条摘要。建议拆分上下文：

```python
ContextBundle(
    base_instructions,
    permissions,
    workspace_state,
    project_knowledge,
    skills,
    memories,
    task_state,
    evidence,
    recent_messages,
)
```

每个 Fragment 包含：

- 来源和信任级别
- 内容 hash / version
- token 预算
- 更新时间
- 是否可缓存
- 是否允许压缩

只有发生变化的 World State 重新生成，避免 Skill、权限和项目说明反复进入摘要。

### 4.2 压缩检查点

参考 Codex 的 Compaction Checkpoint：

- 压缩历史与摘要元数据分离。
- 保留最近真实用户消息。
- 重新注入 canonical initial context。
- 压缩前后 live history 与 persisted history 一致。
- 支持手动压缩、Turn 前压缩和 Turn 中稳定点压缩。
- 记录触发原因、窗口编号、token 前后值和模型。

### 4.3 信任标签

外部网页、MCP 结果和用户项目文件应保留来源类型：

```text
trusted_system
application_context
user_input
untrusted_external
model_generated
```

上下文压缩不能把不可信网页内容总结成系统事实，也不能让工具结果修改权限策略。

---

## 5. 系统级执行安全

### 5.1 ExecutionGateway

当前 `shell` 在宿主权限下运行；cwd 在 workspace 不代表进程只能访问 workspace。
建议统一：

```python
ExecutionRequest(
    argv,
    cwd,
    env,
    filesystem_policy,
    network_policy,
    timeout,
    output_policy,
)

ExecutionResult(
    exit_code,
    stdout,
    stderr,
    duration,
    sandbox_backend,
    audit_id,
)
```

所有 Shell、Verifier、远程和子进程执行都必须经过 ExecutionGateway。

### 5.2 跨平台沙箱后端

| 平台 | 建议后端 | 说明 |
|---|---|---|
| macOS | Seatbelt / `sandbox-exec` | 精确控制读写路径与网络 |
| Linux | Bubblewrap，必要时兼容 Landlock | namespace、只读/可写挂载、网络隔离 |
| Windows | Restricted Token + Job Object；可选提升后端 | 进程树、权限和资源限制 |
| WSL2 | Linux Bubblewrap | 与 Linux 保持一致 |

策略无法准确落实时 fail closed，不能静默扩大权限。

### 5.3 权限 Amendment

审批结果不再只是布尔值，而是最小权限增量：

```python
PermissionAmendment(
    turn_id,
    action="allow_once",
    filesystem=[{"path": "build/", "access": "write"}],
    network=[{"domain": "api.github.com", "access": "connect"}],
    expires_at_turn_end=True,
)
```

用户批准写 `build/` 不等于允许写整个 home；批准一个域名不等于开放全部网络。

---

## 6. 事件可靠性与多前端

### 6.1 有界队列与背压

当前 Runtime 直接同步调用 subscriber callback，慢 UI 可能拖慢 Runtime，异常还会被
吞掉。建议：

- 每个订阅者独立有界队列。
- Runtime 生产事件不等待 UI 渲染。
- 队列满返回结构化 `server_overloaded` 或断开慢消费者。
- 客户端指数退避并从 `after_seq` 重连。
- 关键事件落 SQLite，可重放。

### 6.2 初始化握手

本地 Server 连接后先执行：

```text
initialize(client_info, protocol_version, capabilities)
initialized
```

握手前拒绝其他请求；客户端声明支持的事件、图片、审批和实验特性。

### 6.3 传输

建议顺序：

1. 进程内 Queue（CLI 迁移）。
2. HTTP + SSE（Web MVP）。
3. stdio JSONL（IDE/子进程集成）。
4. WebSocket（确有双向实时需求后再引入）。

---

## 7. AgentLab 分阶段实施路线

### P0：Runtime Protocol 化

1. 新增 `app/protocol/` 与 JSON 类型约束。
2. 定义 `Thread / Turn / Item / EventEnvelope / RuntimeFailure`。
3. 所有事件增加 `schema_version / sequence / timestamp`。
4. RuntimeService 去掉 `__getattr__` 过渡访问。
5. CLI 不再直接访问 `_storage`、`loop_handler`、`session._orch`。
6. 用 append-only item store 持久化关键事实。

验收：

- CLI 只依赖 Protocol Client。
- Event 可 JSON 序列化并通过 Schema 校验。
- 进程重启后可从 sequence 恢复事件流。
- 两个客户端并发操作不同 Thread 不依赖全局 current session。

### P1：统一 TurnEngine 与交互状态机

1. 合并 Legacy Runtime 与 Executor 工具循环。
2. 引入 Direct / Task / Loop Policy。
3. 增加 `waiting_approval / waiting_user_input / suspended`。
4. “修改建议”在同一 Turn 内 steer/resume。
5. 统一工具结果配对、取消和错误分类。
6. 加入 Provider 重试与熔断。

验收：

- 同一审批、图片、取消问题只需修一处。
- 简单请求不调用 Planner。
- 用户 steering 不重新规划已完成任务。
- 503 可取消地退避重试，不重放已执行工具。

### P1：动态工具与上下文

1. 实现 `tool_search` 和按 Turn 工具集。
2. 引入 ContextBundle / Fragment version。
3. 稳定 System/Tool Block 支持 Provider cache。
4. Project Knowledge 和权限状态进入 World State。
5. 压缩检查点可审计、可恢复。

验收：

- 普通 Coding Turn 的 Tool Schema token 至少下降 50%。
- 未授权工具不出现在模型上下文。
- 压缩后目标、任务和权限语义不变。

### P1：ExecutionGateway 与沙箱

1. 抽出统一执行请求/结果。
2. macOS Seatbelt MVP。
3. Linux Bubblewrap MVP。
4. Windows Restricted Token / Job Object MVP。
5. 文件、网络 Permission Amendment。
6. Verifier 和 Shell 全部迁移 Gateway。

验收：

- workspace-only Shell 无法读取 home 中的任意文件。
- network-off 命令无法联网。
- 子进程树能被 timeout/cancel 完整清理。
- 无可用沙箱时按配置 fail closed。

### P2：Fork、多 Agent 与后台任务

1. `/session fork [turn_id]`。
2. Fork 与 Git worktree 绑定。
3. 子 Agent 使用权限子集和独立 Item 流。
4. Guardian/Verifier 只读审查。
5. 后台队列、断点恢复和启动预热。

---

### 7.1 已完成：Protocol v1 基础与队列化事件（阶段 1-2）

当前已完成：

- Thread/Turn/Item/EventEnvelope/RuntimeFailure 和 JSON 值约束。
- JSON Schema 草案、JSONL transport、SQLite append-only Turn/Item/Event。
- Protocol sequence 与 `after_sequence` 游标重放。
- `initialize_client` 握手和客户端能力协商。
- 有界 EventSubscription、慢消费者 overload 和重连提示。
- 常用 TurnEvent/RunEvent 到规范 TurnItem 的映射。
- CLI 首批移除 `_storage` 和 `loop_handler` 私有访问。

下一阶段从文档 P0 的剩余项继续：收口全部 CLI 私有访问、补初始化 transport、将
上下文/Loop 事件全部映射 Item，并开始统一 TurnEngine。

### 7.2 已完成：Runtime Service 边界收口（阶段 3）

本阶段完成：

- `SessionRouter` 提供显式 `storage / session_record / current_session_id` 端口，RuntimeService 不再依赖 Router 的私有存储字段。
- RuntimeService 提供当前 Session、模型只读查询、Loop 命令入口和 Runtime 持久化端口；CLI 不再直接读取 `_storage` 或 `loop_handler`。
- Loop 命令适配器通过显式 `set_loop_handler` 注入；Router 不再承担前端对内部装配细节的隐式转发。
- 保留 legacy Session/Run API 作为迁移兼容层，现有 CLI、Protocol 事件和并发行为不变。

验证结果：

- Runtime/Protocol/SessionRouter 专项测试：35 passed。
- 全量测试：626 passed。
- `compileall`、`git diff --check` 通过。

仍未完成：`loop_commands.py` 内部还直接读取 AgentSession 的 `_orch`、`_ensure_orchestrator` 和 `_emit_run_event`；RunEvent 中的上下文与 Loop 事件仍有 legacy 映射；初始化 transport 尚未提供握手前拒绝请求的完整 Server；统一 TurnEngine、Direct/Task/Loop 自适应路由、Provider retry/circuit breaker、ExecutionGateway 与 OS 沙箱仍待后续阶段实现。

### 7.3 已完成：初始化 JSONL transport 与 Loop 事件协议收口（阶段 4）

本阶段完成：

- 新增 `InitializeRequest`、`JsonlProtocolServer` 和 `ProtocolTransportError`。
- JSONL 连接必须先发送 `initialize`；握手成功前拒绝其它方法，握手后拒绝重复初始化和未实现请求。
- 校验客户端名称、版本、能力数组和协议版本；响应返回 `client_id` 与协商能力。
- `RunEvent` 的上下文预算、压缩、Goal、Loop、验证、修复、Worktree 和 Subagent 事件全部映射为结构化 `TurnItem`。
- 未知运行事件不再静默回退为 `turn.legacy_event`，而是保存为 `runtime.event` 结构化 Item；原始 legacy callback 继续兼容。
- Item payload 在协议边界再次脱敏，避免工具/模型事件中的凭据进入公共事件流。
- 增加握手顺序、重复握手、版本不匹配、非法 JSON 和事件映射回归测试。

仍未完成：独立 stdio 进程服务和完整命令 Submission Queue 尚未接入；统一 TurnEngine、Direct/Task/Loop 自适应路由、Provider retry/circuit breaker、动态工具暴露、ContextBundle、ExecutionGateway 与 OS 沙箱仍待后续阶段。

---

### 7.4 已完成：Direct / Task / Loop 本地模式路由（阶段 5）

本阶段完成：

- 新增 `ExecutionMode`、`SessionState` 和无模型调用的 `ModeRouter`。
- `mode=auto` 时，简单问答/单步读取走 Direct，不调用 Planner；明显多文件、多步骤、修改并测试等请求走 Task。
- 明确包含 GoalSpec、成功标准、验收标准或 `/loop` 的请求标记为 Loop；Loop 的完整验证生命周期仍由 `/goal` / `/loop` Handler 驱动，不在普通 chat 中误启动。
- Profile 支持 `mode: auto/direct/task`；未配置时保持旧 `orchestrate` 语义，`orchestrate: false` 始终走 Direct。
- 新增 `mode_selected` RunEvent，并映射为 `turn.mode` TurnItem；Direct 与 Task 两条路径都会发出，CLI 显示当前执行模式。
- 多附件 + 明确操作自动进入 Task；无开放任务时普通 `/resume` 不会意外恢复旧任务。
- 增加模式选择、Planner 绕过、Task 路由、profile 校验和协议事件测试。

验证：模式专项测试 26 passed（18 个模式路由测试 + 8 个协议事件测试）；全量单元测试 668 passed。

测试覆盖：
- **模式路由回归测试**（`tests/unit/test_mode_router.py`）：
  - 基础模式选择（Direct/Task/Loop）
  - 边界情况：空输入、单/多附件、大小写不敏感、优先级覆盖
  - Session 状态兼容性：字典与 SessionState 对象
  - orchestrate_enabled=False 强制 Direct
  - 中英文任务标记识别，英文动作词按整词匹配
  - 活跃 GoalSpec 叠加操作请求时留在 Loop
  - 普通对话保持 Direct，避免误判
- **协议事件测试**（`tests/unit/test_protocol_events.py`）：
  - 通过 `chat()` 验证 Direct 与 Task 两条路径都发出 MODE_SELECTED
  - MODE_SELECTED payload 结构与 execution_mode 一致性
  - RunEvent dataclass 字段访问与默认值
  - 嵌套 payload 支持（为 Turn/Item 演进准备）
  - session_state 快照在事件中的传递

仍未完成：统一 TurnEngine（目前 Direct 仍是 legacy 循环、Task 仍复用 Orchestrator）；Loop 专用策略、动态工具暴露、Provider retry/circuit breaker、独立 stdio Submission Queue、ContextBundle、ExecutionGateway 与 OS 沙箱仍待后续阶段。

---

    envelopes.py
    commands.py
    events.py
    items.py
    errors.py
    schema.py

  runtime/
    service.py
    turn_engine.py
    mode_router.py
    thread_manager.py
    turn_context.py
    event_bus.py
    item_store.py

  context/
    bundle.py
    fragments.py
    world_state.py
    compaction.py
    provider_cache.py

  execution/
    gateway.py
    policy.py
    result.py
    macos_seatbelt.py
    linux_bwrap.py
    windows_token.py

  providers/
    capabilities.py
    retry.py
    circuit_breaker.py
```

迁移期间保留旧 import adapter，但新模块不能依赖 CLI。

---

## 9. 关键指标

| 目标 | 指标 |
|---|---|
| 简单请求效率 | Direct 请求不调用 Planner，首 token 延迟下降 |
| Tool Schema 成本 | 常规 Turn 固定工具 token 至少下降 50% |
| Runtime 可靠性 | Turn 崩溃后可恢复到最后一个已持久化 Item |
| 多前端一致性 | CLI/Web/TUI 对同一事件流渲染结果一致 |
| 审批正确性 | waiting 状态下不继续模型调用或执行工具 |
| Provider 稳定性 | 可重试错误自动退避，取消响应及时 |
| Shell 安全 | workspace policy 由 OS 沙箱真实执行 |
| 上下文质量 | 压缩后目标、约束、权限、任务和证据完整保留 |

---

## 10. 不应照搬的部分

- 不把 Codex 的 OpenAI 专属认证、模型目录和内部 Extension 体系直接移入 AgentLab。
- 不要求 AgentLab 改写为 Rust；先通过稳定边界修复架构问题。
- 不一次性引入所有 App Server API；优先覆盖 Thread/Turn/Approval/Cancel/Event。
- 不为了形式复制复杂沙箱策略；每个平台先做可验证的最小安全后端。
- 不把多 Agent 提前到 Protocol、TurnEngine 和 ExecutionGateway 之前。

---

## 11. 参考资料

- [OpenAI Codex GitHub](https://github.com/openai/codex)
- [Codex Core](https://github.com/openai/codex/tree/main/codex-rs/core)
- [Codex Protocol](https://github.com/openai/codex/tree/main/codex-rs/protocol)
- [Codex App Server](https://github.com/openai/codex/tree/main/codex-rs/app-server)
- [Codex Core 沙箱说明](https://github.com/openai/codex/blob/main/codex-rs/core/README.md)
- [Codex 安装与源码构建](https://github.com/openai/codex/blob/main/docs/install.md)

调研结论基于 2026-08-24 获取的公开仓库内容；上游架构会持续变化，实施时应以固定
commit/tag 的协议和源码为准。
