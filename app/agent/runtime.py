"""Agent Runtime —— 驱动"模型 → 工具 → 模型"的多轮对话循环。"""
from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Optional

from app.agent.approval import ApprovalPolicy, AutoApprove
from app.models.router import ModelRouter
from app.tools.registry import ToolRegistry

DEFAULT_SYSTEM_PROMPT = """你是 AgentLab,一个本地编码助手。
- 用户用中文与你对话时,回复也用中文。
- 你可以使用提供的工具读写本地文件、列目录,优先用工具拿到第一手信息再回答。
- 写代码时简洁优先,不要过度解释。
- 路径不清楚时,先用 list_dir 看一眼,再决定下一步。
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

    def reset(self) -> None:
        self.messages = []
        self.last_turn_usage = {"input_tokens": 0, "output_tokens": 0}
        self.cumulative_usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_turn_seconds = 0.0
        self.cumulative_seconds = 0.0

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

                # provider_payload 必须原样追加到历史（Anthropic 要求 content blocks 完整回放）
                self.messages.append({"role": "assistant", "content": resp.provider_payload})

                # 文本未被流式打印过才补发 text 事件，避免重复
                if resp.text and not text_streamed:
                    self._on_event(TurnEvent(kind="text", text=resp.text))

                if not resp.tool_calls:
                    return resp.text

                tool_results_block: list[dict[str, Any]] = []
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

                    # Anthropic 格式的 tool_result；OpenAI 格式由 compatible_adapter 在下轮处理
                    tool_results_block.append({
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": output,
                        "is_error": is_error,
                    })

                self.messages.append({"role": "user", "content": tool_results_block})

            return "(达到最大步数仍未给出最终答案)"
        finally:
            self.last_turn_seconds = time.monotonic() - turn_start
            self.cumulative_seconds += self.last_turn_seconds
