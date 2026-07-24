"""Agent Runtime —— 驱动"模型 → 工具 → 模型"的多轮对话循环。"""
from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Optional

from app.agent.approval import ApprovalPolicy, AutoApprove
from app.agent.cancel import CancelToken
from app.agent.events import RunEvent
from app.agent.tasks import TaskStore
from app.models.protocol import ToolResult
from app.models.router import ModelRouter
from app.tools.registry import ToolRegistry

DEFAULT_SYSTEM_PROMPT = """你是 AgentLab,一个本地编码助手。

【行为准则——最重要】
- 主动调查,不要反问。用户让你"看项目""分析代码"时,直接用工具读明白再回答,
  绝不要回一句"请问你想了解什么"或列一堆选项让用户选。先调查,后结论。
- 缺信息时用工具去拿,不要问用户:路径不清楚就 list_dir;想知道功能就 read_file
  读 README、入口文件、关键模块。连续调用工具直到信息足够。
- 找代码用 code_search,不要用 shell 拼 grep/find:搜函数名/字符串用 mode=text,
  搜正则用 regex,找文件用 file,找定义用 symbol。拿到命中再用 read_file 精读。
- 不要重复你上一条已经说过的话。如果发现自己在重复,改为调用工具或给出最终结论。

【改文件必读】
- 改/删几行、改配置:用 edit_file(old_str→new_str),不要 write_file 覆盖整文件,
  更不要用 shell 重定向 / sed -i / python -c 改文件。edit_file 会在审批前展示
  彩色 diff,用户能看清改了什么再确认。
- 新建文件才用 write_file。追加内容用 edit_file(old_str="", new_str=要追加的内容)。
- shell 工具只用于查看(ls / grep / git status)、安装依赖、跑测试。禁止用它改文件。

【常规约定】
- 用户用中文对话时,回复也用中文。
- 写代码时简洁优先,不要过度解释。
- 任务复杂(需要 3 步以上,或涉及多个文件)时,先用 todo_write 列出子任务清单,
  然后边做边把任务从 pending → in_progress → completed,让用户看见进度。简单任务不必用。
- 如果工具返回"User denied execution",说明用户拒绝了这次操作,不要立即重试,
  而是向用户简短说明,询问替代方案或直接停下。

【浏览器工具(如果可用)】
- 有 browser_* 工具时,操作网页的正确顺序:先 browser_navigate 打开 URL,
  再 browser_snapshot 拿到页面快照和元素 ref(引用),然后用 ref 做 browser_click /
  browser_type / browser_select_option。不要凭空猜 CSS selector,以 snapshot 里的 ref 为准。
- 每次点击/输入改变页面后,需要时再 browser_snapshot 重新观察,再决定下一步。
- 登录、支付、发布、删除、上传等敏感动作执行前要先向用户确认。"""


def build_system_prompt(workspace: str | None = None) -> str:
    """在默认 prompt 基础上注入当前工作目录。

    小模型(qwen2.5-coder:7b 等)不会凭空知道自己在哪个目录,因此用户说
    "看下当前项目"时会反问"项目在哪"。把 workspace 根目录显式写进 system
    prompt,模型才能直接 list_dir 该目录而不是反问。workspace 为空时退回纯
    默认 prompt(保持向后兼容)。
    """
    if not workspace:
        return DEFAULT_SYSTEM_PROMPT
    return (
        f"{DEFAULT_SYSTEM_PROMPT}\n\n"
        f"【当前工作目录】{workspace}\n"
        f"用户说\"当前项目\"\"这个项目\"时,默认指这个目录。无需向用户确认路径。"
    )

DENIED_MESSAGE = (
    "User denied execution of this tool. Do not retry without first confirming with the user."
)

ProgressFn = Callable[[str], ContextManager[Any]]


@dataclass
class TurnEvent:
    """单步事件，通过 on_event 回调传给 CLI。

    kind: "text" | "tool_call" | "tool_result" | "tool_denied"
    """
    kind: str
    text: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_output: str = ""
    tool_error: bool = False
    elapsed_seconds: float = 0.0


class AgentSession:
    """一次完整的对话会话，维护消息历史和统计数据。"""

    def __init__(
        self,
        llm: ModelRouter,
        tools: ToolRegistry,
        approval: Optional[ApprovalPolicy] = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_steps: int = 8,
        on_event: Optional[Callable[[TurnEvent], None]] = None,
        progress: Optional[ProgressFn] = None,
        task_store: Optional[TaskStore] = None,
        closeables: Optional[list[Any]] = None,
        *,
        orchestrate: bool = False,
        planner: Optional[Any] = None,
        on_run_event: Optional[Callable[[RunEvent], None]] = None,
        context_manager: Optional[Any] = None,
    ):
        self.llm = llm
        self.tools = tools
        self.approval: ApprovalPolicy = approval or AutoApprove()
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.messages: list[dict[str, Any]] = []
        self._on_event = on_event or (lambda e: None)
        self._progress: ProgressFn = progress or (lambda label: nullcontext())
        self.last_turn_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self.cumulative_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self.last_turn_seconds: float = 0.0
        self.cumulative_seconds: float = 0.0
        # 服务器实际返回的模型 ID。代理可能把 claude-opus-4-9 静默映射到
        # claude-3-5-sonnet,这里记下来给 CLI 在 banner / 警告里展示
        self.last_actual_model: Optional[str] = None
        # 任务清单:模型用 todo_write 工具维护,CLI 渲染到 spinner 上方
        self.task_store: TaskStore = task_store or TaskStore()
        # 需要在会话结束时收尾的资源(如 MCPManager),退出时按序调用其 .stop()/.close()
        self._closeables: list[Any] = closeables or []
        # ── 编排路径(可选)────────────────────────────────────────────────────
        # orchestrate=True 时,chat() 委托给 Orchestrator(Planner/Executor/Replanner),
        # 否则走下面的 legacy 单轮工具循环(默认,保持所有既有测试行为不变)。
        self._orchestrate = orchestrate
        self._planner = planner
        self._on_run_event = on_run_event or (lambda e: None)
        self._orch = None  # 懒构建(见 _ensure_orchestrator)
        # 上下文预算/压缩协调者(可选,§7.3)。交给 Orchestrator 在稳定点调用。
        # None 时编排路径不做预算检查与压缩(默认,既有测试不受影响)。
        self.context_manager = context_manager
        # 编排 run 的目标与结果状态,供 SessionRouter.persist_current 写 runs 审计
        self.last_goal: str = ""
        self.last_run_status: str = ""

    def close(self) -> None:
        """释放会话持有的外部资源(MCP server 进程等)。重复调用安全。"""
        for c in self._closeables:
            stop = getattr(c, "stop", None) or getattr(c, "close", None)
            if stop is not None:
                try:
                    stop()
                except Exception:
                    pass
        self._closeables = []

    def reset(self) -> None:
        # 就地清空 messages(而非重新赋值),保持 Orchestrator 共享的引用有效
        self.messages.clear()
        self.last_turn_usage = {"input_tokens": 0, "output_tokens": 0}
        self.cumulative_usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_turn_seconds = 0.0
        self.cumulative_seconds = 0.0
        self.last_goal = ""
        self.last_run_status = ""
        self.task_store.clear()

    def _ensure_orchestrator(self):
        """懒构建 Orchestrator,并让它与本 session 共享 messages / task_store。

        懒构建是因为 SessionRouter.switch 会在构造后整体替换 self.messages
        (从 SQLite 读回历史)。等首次 chat 时再绑定,确保拿到的是最终的 list。
        """
        from app.agent.orchestrator import Orchestrator  # 延迟导入避免环依赖
        if self._orch is None:
            self._orch = Orchestrator(
                llm=self.llm,
                tools=self.tools,
                approval=self.approval,
                system=self.system_prompt,
                max_steps=self.max_steps,
                task_store=self.task_store,
                planner=self._planner,
                on_event=self._on_run_event,
                progress=self._progress,
                messages=self.messages,
                context_manager=self.context_manager,
            )
        # 每次都重新指向当前 messages:switch/reset 可能换过引用
        self._orch.messages = self.messages
        return self._orch

    def chat(self, user_input: str, *, cancel: Optional[CancelToken] = None,
             resume: bool = False) -> str:
        """处理一轮用户输入。

        orchestrate=True 时委托给 Orchestrator(规划→执行→重规划,产出 RunEvent);
        否则走 legacy 单轮循环。cancel 仅编排路径生效(legacy 路径忽略)。
        resume=True 时继续上一轮未完成的任务(失败任务重置为 pending),仅编排路径生效。
        """
        if self._orchestrate:
            return self._chat_orchestrated(user_input, cancel=cancel, resume=resume)
        return self._chat_legacy(user_input)

    def _chat_orchestrated(self, user_input: str, *, cancel: Optional[CancelToken] = None,
                           resume: bool = False) -> str:
        self.last_turn_usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_turn_seconds = 0.0
        self.last_goal = user_input
        self.last_run_status = ""
        turn_start = time.monotonic()
        try:
            orch = self._ensure_orchestrator()
            answer = orch.run(user_input, cancel=cancel, resume=resume)
            # 把 run 级统计拷回 session,供 CLI 展示 / 持久化
            self.last_turn_usage = dict(orch.last_run_usage)
            for k in ("input_tokens", "output_tokens"):
                self.cumulative_usage[k] += orch.last_run_usage.get(k, 0)
            if orch.last_actual_model:
                self.last_actual_model = orch.last_actual_model
            self.last_run_status = orch.last_run_status
            return answer
        finally:
            self.last_turn_seconds = time.monotonic() - turn_start
            self.cumulative_seconds += self.last_turn_seconds

    def _chat_legacy(self, user_input: str) -> str:
        self.messages.append({"role": "user", "content": user_input})
        self.last_turn_usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_turn_seconds = 0.0
        turn_start = time.monotonic()

        try:
            for _ in range(self.max_steps):
                text_streamed = False
                with self._progress("thinking") as handle:
                    on_progress = getattr(handle, "update", None)
                    raw_on_text = getattr(handle, "on_text", None)
                    raw_on_thinking = getattr(handle, "on_thinking", None)

                    def on_text_delta(delta: str) -> None:
                        nonlocal text_streamed
                        text_streamed = True
                        if raw_on_text is not None:
                            raw_on_text(delta)

                    resp = self.llm.create_message(
                        messages=self.messages,
                        tools=self.tools.schemas() or None,
                        system=self.system_prompt,
                        on_progress=on_progress,
                        on_text_delta=on_text_delta if raw_on_text else None,
                        on_thinking_delta=raw_on_thinking,
                    )

                for k in ("input_tokens", "output_tokens"):
                    v = resp.usage.get(k, 0)
                    self.last_turn_usage[k] += v
                    self.cumulative_usage[k] += v

                # 记下服务器实际使用的模型 ID(可能被代理静默映射到别的型号)
                if resp.actual_model:
                    self.last_actual_model = resp.actual_model

                # provider_payload 是 list[dict],由 adapter 决定追加哪几条 message
                # (Anthropic 把多个 content block 合在一条 assistant 里;
                #  OpenAI Responses 可能把 text 块和 function_call 块拆成多条)
                self.messages.extend(resp.provider_payload)

                # 文本未被流式打印过才补发 text 事件，避免重复
                if resp.text and not text_streamed:
                    self._on_event(TurnEvent(kind="text", text=resp.text))

                if not resp.tool_calls:
                    return resp.text

                tool_results: list[ToolResult] = []
                for call in resp.tool_calls:
                    self._on_event(TurnEvent(
                        kind="tool_call",
                        tool_name=call.name,
                        tool_input=call.arguments,
                    ))

                    tool = self.tools.get(call.name)
                    approval_action = (
                        tool.approval_action(call.arguments) if tool else None
                    )
                    if approval_action and not self.approval.request(
                        approval_action,
                        call.arguments,
                    ):
                        output = DENIED_MESSAGE
                        is_error = False
                        self._on_event(TurnEvent(kind="tool_denied", tool_name=call.name))
                    else:
                        t0 = time.monotonic()
                        output, is_error = self.tools.execute(
                            call.name,
                            call.arguments,
                            approved_action=approval_action,
                        )
                        # 进入历史前截断超大输出(与编排路径一致,见 executor)。
                        from app.agent.executor import _truncate_tool_output
                        output = _truncate_tool_output(output)
                        self._on_event(TurnEvent(
                            kind="tool_result",
                            tool_name=call.name,
                            tool_input=call.arguments,
                            tool_output=output,
                            tool_error=is_error,
                            elapsed_seconds=time.monotonic() - t0,
                        ))

                    tool_results.append(ToolResult(
                        tool_call_id=call.id,
                        output=output,
                        is_error=is_error,
                    ))

                # 工具结果的回传格式由 adapter 决定(Anthropic / OpenAI Chat /
                # OpenAI Responses 三家不一样),Runtime 不关心
                self.messages.extend(self.llm.format_tool_results(tool_results))

            return "(达到最大步数仍未给出最终答案)"
        finally:
            self.last_turn_seconds = time.monotonic() - turn_start
            self.cumulative_seconds += self.last_turn_seconds
