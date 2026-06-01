"""Agent Runtime —— 驱动"模型 → 工具 → 模型"的多轮对话循环。"""
from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Optional

from app.agent.approval import ApprovalPolicy, AutoApprove
from app.agent.tasks import TaskStore
from app.models.protocol import ToolResult
from app.models.router import ModelRouter
from app.tools.registry import ToolRegistry

DEFAULT_SYSTEM_PROMPT = """你是 AgentLab,一个本地编码助手。
- 用户用中文与你对话时,回复也用中文。
- 你可以使用提供的工具读写本地文件、列目录、执行 shell 命令,
  优先用工具拿到第一手信息再回答。
- 写代码时简洁优先,不要过度解释。
- 路径不清楚时,先用 list_dir 看一眼,再决定下一步。
- 任务复杂(需要 3 步以上,或涉及多个文件)时,先用 todo_write 列出
  子任务清单,然后边做边把对应任务从 pending → in_progress → completed,
  让用户看见进度。简单任务不必用 todo_write。
- 如果工具返回"User denied execution",说明用户拒绝了这次操作,不要立即重试,
  而是向用户简短说明,询问替代方案或直接停下。"""

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

    def reset(self) -> None:
        self.messages = []
        self.last_turn_usage = {"input_tokens": 0, "output_tokens": 0}
        self.cumulative_usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_turn_seconds = 0.0
        self.cumulative_seconds = 0.0
        self.task_store.clear()

    def chat(self, user_input: str) -> str:
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
                    if tool and tool.requires_approval and not self.approval.request(call.name, call.arguments):
                        output = DENIED_MESSAGE
                        is_error = False
                        self._on_event(TurnEvent(kind="tool_denied", tool_name=call.name))
                    else:
                        t0 = time.monotonic()
                        output, is_error = self.tools.execute(call.name, call.arguments)
                        self._on_event(TurnEvent(
                            kind="tool_result",
                            tool_name=call.name,
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
