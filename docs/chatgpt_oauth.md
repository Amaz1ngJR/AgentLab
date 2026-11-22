# ChatGPT OAuth 模型接入

AgentLab 使用自己的 ChatGPT OAuth 注册访问 Responses API。`chatgpt_codex` profile
配置为 `gpt-6.2-sol`，推理强度为 `medium`。任务编排、工具执行、记忆、审批和本地会话由 AgentLab 管理。
该入口不会调用 Codex CLI，也不会读取 `~/.codex/auth.json`。

## 使用

先安装更新后的 `requirements.txt`，然后执行：

```bash
agentlab auth login chatgpt
agentlab auth status chatgpt
agentlab auth models chatgpt
agentlab auth test chatgpt
agentlab --profile chatgpt_codex
```

登录会打开系统浏览器，显示 Continue with ChatGPT，要求授权 AgentLab 使用
ChatGPT 额度。账号与工作区是否支持该预览入口，以授权结果、模型列表和实际请求
为准。`auth test` 会产生真实模型用量，先测试文本，再执行一次无副作用的本地
echo 工具调用及结果回放。不自动改用 API Key 计费。

其他 OAuth profile 可用 `agentlab auth test chatgpt --profile <名称>` 验证。
该命令会拒绝 API Key provider，防止误用另一条计费路径。
`auth models` 同时显示账号模型目录中的上下文窗口。启动界面显示账号标签、
生效窗口及其来源；未登录或续期失效会先给出修复提示。

若想设为默认，在 `.env` 设置：

```dotenv
ACTIVE_PROFILE=chatgpt_codex
CHATGPT_ACCOUNT=default
```

多账号使用独立标签，例如 `agentlab auth login chatgpt --account personal`。
账号标签标识独立注册，不根据邮箱自动合并。新运行通过 `CHATGPT_ACCOUNT`
选择账号，已创建运行保留自己的账号引用。重新登录同一标签必须匹配原身份。

退出：`agentlab auth logout chatgpt`。尝试远程撤销后清除本地令牌，保留注册
映射；若远程撤销未确认，按提示到 ChatGPT 设置断开 AgentLab。

## 存储与续期

默认目录为 `~/.agentlab/auth/chatgpt/`，可通过进程环境变量
`CHATGPT_AUTH_DIR` 覆盖。不要将目录指向代码仓库。令牌不写入 `.env`、
`LLMConfig`、模型请求快照或会话历史。Unix 目录权限为 0700，凭据文件为
0600；文件权限不能防止以同一操作系统用户运行的任意代码访问凭据。

PKCE、state、nonce 和 JWKS 签名校验用于登录验证。身份校验完成后才替换
账号凭据。access token 临近过期时按账号加跨进程锁刷新，轮换令牌一起原子写入。
网络故障保留凭据；确认 refresh token 失效则清除令牌并要求重新登录。
退出或重新登录期间，同账号请求可能等待凭据锁。

## 协议与执行

上下文窗口优先使用 `LLM_CONTEXT_SIZE` 或 profile 的 `context_size`；未设置时，
按当前账号 `/v1/models` 返回的 `context_window` 解析并缓存。查询失败才回退到
64,000 tokens，可在 `/context` 中查看窗口及来源。当前账号实测 `gpt-6.1-sol`
返回 500,000 tokens；不要把通用 API 模型页面的最大窗口当作账号授权窗口。

- 固定使用官方 `https://api.openai.com/v1/responses`，每次请求新建认证客户端。
- 所有入口（包括规划、记忆压缩使用的 `chat()`）均消费 SSE 流。
- 固定 `store=false`、`stream=true`，每轮发送本地管理的必要历史。
- 按原顺序回放带 `encrypted_content` 的 reasoning item（保留 ID 和 summary），
  同时保留 assistant 消息的 commentary/final_answer phase，以及工具 namespace。
  仅有摘要、缺少加密状态的 reasoning 不作为可恢复状态回放。加密状态不解密，
  也不作为普通文本发送给上下文压缩模型；压缩旧历史后不承诺继续复用旧推理。
- 主模型请求把 AgentLab 指令作为 developer 消息保存在历史中；Skill 等指令
  变化时追加新的完整指令，明确替代之前的临时工作流，不修改已经发送的消息。
- 工具通过 developer-role `additional_tools` 在首次需要时追加定义；之后工具
  的移除、重排或暂时禁用只调整当前请求的 allowed_tools / none，旧定义保留。
  本地仍校验返回调用属于当前允许列表，执行和审批权限保持原有约束。
  同名工具 schema 改变时追加带内容哈希的版本名，允许列表只开放当前版本；
  返回调用映射回本地原工具名，历史中的版本名保持不变。运行期预算变化无需新建会话，
  本地参数验证和审批仍检查当前工具定义，不因历史中存在旧版本而扩大权限。
- 前缀项随消息历史保存，重启、恢复和同账号子任务都可回放；请求取消或失败时，
  隔离工作线程不会将追加项提交到活跃历史。压缩和跨模型切换仍会形成新的前缀。
- 不发送 temperature、top_p、max_output_tokens、previous_response_id 等
  当前 OAuth 路线不支持的字段。因此 `LLM_MAX_TOKENS` 不控制此入口的输出。
- 本地函数工具包装进 `agentlab` namespace，历史回放保留调用 namespace。
- 模型只能请求当前声明的工具；工具仍经 AgentLab 既有审批和执行模块处理。
- 参数流生成期间持续更新输出估算，完成后以实际 usage 和 final 标记结算；
  reasoning summary（若服务返回）进入现有展示接口。
- CLI 的 `[stats]` 在服务提供明细时显示 `cached` 和 `reasoning`。回合记录也保存
  已返回的用量明细、请求模型、推理强度和服务返回的模型。推理 token 已含在输出总量里，
  缓存命中 token 已含在该路线的输入总量里。
- 只有完整 `response.completed` 才提交 ModelResponse；流断开、失败、
  incomplete、参数损坏或空结果均不能冒充任务成功。
- 额度限制与账号不支持不重试；明确的暂时不可用按现有有限退避策略重试。
  中断时不自动重发已执行的本地工具。
- 流内失败保留 request ID、HTTP 状态和错误参数，普通临时限流遵守 Retry-After；
  额度耗尽继续作为不可重试错误处理。
- 全前缀子任务仅允许同 provider、同模型、同账号，仍保留子任务工具限制，
  不承诺整条请求具有相同缓存前缀。重复或缺失的工具调用 ID 阻止整轮执行。

## 等待长命令

下载、安装和构建等非交互命令可用 `terminal_open` 的 `wait_for_exit=true` 模式。
工具会在本地等退出或超时，并返回退出码。默认等待 120 秒，可用 `timeout` 指定
最多 300 秒。若超时且进程仍在运行，向同一个会话调用 `terminal_send` 并省略
`input`，即可在本地继续等待。交互式终端仍支持按输出静默时间返回。

## 验证

离线检查：

```bash
python -m pytest tests/unit/test_chatgpt_oauth.py tests/unit/test_provider_retry.py tests/unit/test_cli_entrypoint.py tests/unit/test_context_window.py -q
```

覆盖注册与重新登录、PKCE/回调、身份校验、刷新竞争、令牌失效、退出、协议约束、
工具回放、损坏参数、取消及流式错误。真实账号可用性需另外执行上面的登录和
`auth test`；离线测试通过不表示账号已经获得该预览能力。

官方协议参考：

- [登录与注册](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [账号、续期与撤销](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [模型与推理](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [当前预览限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [错误恢复](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)

## 本次接入验证（2026-11-11）

- 已完成独立 ChatGPT OAuth 授权，账号模型列表包含 `gpt-6.1-sol`。
- `auth test` 已通过真实文本与本地 echo 工具调用、结果回放测试。
- 完整 CLI 使用 `chatgpt_codex` profile（medium 推理）在临时工作区调用
  `read_file`，正确返回 `AGENTLAB_OAUTH_RUNTIME_OK`；未使用自动审批。
- 原有 OpenAI adapter 的 6 项 SDK 状态机兼容测试在 OpenAI SDK 3.49.0 下失败，
  已在未修改的 HEAD 副本复现；该问题独立于本次 OAuth adapter。
- 本轮在用户实际 Python 3.11 / OpenAI SDK 2.38.0 环境中，分两组共 315 项
  相关检查通过，包含原有 OpenAI adapter、OAuth、上下文压缩、记忆、审批和子任务。
  真实文本与工具调用闭环再次通过。

后续可扩展项：账号模型目录驱动的模型选择界面、图片端到端验证，以及实际
跨 access token 到期的长任务观察。当前已经用离线并发/进程测试验证刷新轮换，
没有为测试而强制刷新、撤销或破坏现有账号授权。

## 原生压缩能力验证（2026-11-12）

使用当前 OAuth 账号与 `gpt-6.1-sol`，仅发送合成文本，实测结果：

| 路线 | 结果 |
|---|---|
| `POST /v1/responses/compact` | HTTP 401，`hardened_oauth_rule_missing`，该操作未获当前凭据授权 |
| `/v1/responses` + `context_management` | HTTP 200、完整 `response.completed`，返回加密 `compaction` 项 |
| 将流内最后一个 compaction 项及其后内容用于下一次请求 | HTTP 200，正确恢复测试标记 |

探测使用 `store=false`、`stream=true`，测试压缩阈值为 1024，仅用于用少量文本
触发能力验证，不是生产阈值。结果只代表此次账号/模型/路线，不能外推独立端点
已对其他账号开放。目前没有自动启用流内原生压缩；适配器还需完整接入压缩项的
回放、历史窗口替换、预算和取消处理后才能替代现有摘要流程。

现有自定义压缩现在会重建 AgentLab 管理的运行状态：只保留一份当前完整指令，
合并当前有效工具定义，清理旧指令及不再使用的工具版本。近期仍保留的工具调用
引用的旧版本会一并保留，待调用离开近期窗口后再清理。用户或其他来源的显式
developer 指令不因这项清理被自动删除。请求允许列表、本地参数校验和审批不变。
这项重建在压缩候选预算校验通过后才提交，失败或取消不会改写原始历史。

## 压缩超时

默认按输入量、分片和合并调用数分配 600–1800 秒整体时间预算；成功后立即结束，
进度或重试不会无限续期。可用 `AGENTLAB_COMPACTION_TIMEOUT_SECONDS=1800` 显式覆盖。
压缩使用独立 Router 配置，将 SDK 请求超时扩展到本次剩余时间，普通对话的
`LLM_TIMEOUT_SECONDS` 保持不变；子任务的预算包装不被绕过。取消和整体截止时间
仍生效，超时保留原始历史，迟到响应不会覆盖会话。修改配置后重启生效。
