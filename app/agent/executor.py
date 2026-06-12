"""Executor —— 执行单个任务:驱动"模型 → 工具 → 模型"循环直到该任务产出结果。

与 runtime.AgentSession.chat 的关系:
  AgentSession 是面向"一整轮对话"的循环(CLI 当前主路径)。Executor 是面向
  "TaskStore 里的一个子任务"的循环:它把任务目标作为指令注入共享的 messages,
  跑有限步工具循环,把结果作为 evidence 返回给编排器。多个任务共享同一份
  messages,所以后做的任务能看到先做任务积累的上下文。

输出 RunEvent(message_delta / tool_requested / approval_required / tool_completed /
tool_denied),由编排器统一转发给 UI。任务级状态(completed/failed/blocked)由
Replanner 根据这里返回的 TaskOutcome 决定,Executor 自己不写 TaskStore。
"""
from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Optional

from app.agent import events
from app.agent.approval import ApprovalPolicy, AutoApprove
from app.agent.cancel import CancelToken
from app.agent.events import RunEvent
from app.agent.tasks import BLOCKED, COMPLETED, FAILED, Task
from app.models.protocol import ToolResult
from app.models.router import ModelRouter
from app.tools.registry import ToolRegistry

DENIED_MESSAGE = (
    "User denied execution of this tool. Do not retry without first confirming with the user."
)

# progress 工厂签名:接收 label,返回一个上下文管理器(可带 .update / .on_text)
ProgressFn = Callable[[str], ContextManager[Any]]

# 注入到 messages 的任务指令模板:告诉模型"现在只聚焦这一个子任务"
_TASK_DIRECTIVE = (
    "【当前子任务】{content}\n"
    "请只聚焦完成这一个子任务。完成后用一句话说明结果即可,不要展开成最终总结。"
)


@dataclass
class TaskOutcome:
    """Executor 执行完一个任务后的结论,交给 Replanner 决定如何回写 TaskStore。

    status   - 建议状态:completed / failed / blocked。
    evidence - 给后续任务/审计看的产出摘要(通常是模型最后那段文本)。
    error    - 失败/阻塞原因(status != completed 时填)。
    text     - 模型本任务最后一段自然语言输出(可作为最终答复的素材)。
    """
    status: str
    evidence: str = ""
    error: str = ""
    text: str = ""
    tool_calls_made: int = 0


class Executor:
    """按任务目标驱动有限步工具循环。"""

    def __init__(
        self,
        llm: ModelRouter,
        tools: ToolRegistry,
        approval: Optional[ApprovalPolicy] = None,
        on_event: Optional[Callable[[RunEvent], None]] = None,
        progress: Optional[ProgressFn] = None,
    ):
        self._llm = llm
        self._tools = tools
        self._approval: ApprovalPolicy = approval or AutoApprove()
        self._emit = on_event or (lambda e: None)
        self._progress: ProgressFn = progress or (lambda label: nullcontext())

    def run_task(
        self,
        task: Task,
        messages: list[dict[str, Any]],
        *,
        system: str,
        max_steps: int,
        cancel: Optional[CancelToken] = None,
        usage_acc: Optional[dict[str, int]] = None,
        on_actual_model: Optional[Callable[[str], None]] = None,
    ) -> TaskOutcome:
        """执行一个任务。就地把对话追加进共享 messages,返回 TaskOutcome。

        max_steps 是分给"这一个任务"的步数预算;编排器从全局预算里切一块给它。
        usage_acc 若传入,会把每轮 token 用量累加进去(供 run 级统计)。
        on_actual_model 在拿到服务器真实模型 ID 时回调一次(揭穿代理静默映射)。
        """
        messages.append({"role": "user", "content": _TASK_DIRECTIVE.format(content=task.content)})

        last_text = ""
        tool_calls_made = 0

        for _ in range(max(1, max_steps)):
            if cancel is not None:
                cancel.raise_if_cancelled()

            text_streamed = False
            with self._progress("thinking") as handle:
                on_progress = getattr(handle, "update", None)
                raw_on_text = getattr(handle, "on_text", None)

                def on_text_delta(delta: str) -> None:
                    nonlocal text_streamed
                    text_streamed = True
                    if raw_on_text is not None:
                        raw_on_text(delta)

                resp = self._llm.create_message(
                    messages=messages,
                    tools=self._tools.schemas() or None,
                    system=system,
                    on_progress=on_progress,
                    on_text_delta=on_text_delta if raw_on_text else None,
                )

            if usage_acc is not None and getattr(resp, "usage", None):
                for k in ("input_tokens", "output_tokens"):
                    usage_acc[k] = usage_acc.get(k, 0) + resp.usage.get(k, 0)
            if on_actual_model is not None and getattr(resp, "actual_model", None):
                on_actual_model(resp.actual_model)

            messages.extend(resp.provider_payload)

            if resp.text:
                last_text = resp.text
                # 文本若已被 spinner 流式打印过,就不再发 message_delta(避免重复)
                if not text_streamed:
                    self._emit(RunEvent(kind=events.MESSAGE_DELTA, text=resp.text,
                                        task_id=task.id))

            # 没有工具调用 = 模型认为这个子任务已经做完
            if not resp.tool_calls:
                return TaskOutcome(
                    status=COMPLETED,
                    evidence=last_text.strip(),
                    text=last_text.strip(),
                    tool_calls_made=tool_calls_made,
                )

            tool_results: list[ToolResult] = []
            denied_any = False
            for call in resp.tool_calls:
                if cancel is not None:
                    cancel.raise_if_cancelled()
                tool_calls_made += 1
                self._emit(RunEvent(kind=events.TOOL_REQUESTED, task_id=task.id,
                                    tool_name=call.name, tool_input=call.arguments))

                tool = self._tools.get(call.name)
                needs_approval = bool(tool and tool.requires_approval)
                if needs_approval:
                    self._emit(RunEvent(kind=events.APPROVAL_REQUIRED, task_id=task.id,
                                        tool_name=call.name, tool_input=call.arguments))

                if needs_approval and not self._approval.request(call.name, call.arguments):
                    output, is_error = DENIED_MESSAGE, False
                    denied_any = True
                    self._emit(RunEvent(kind=events.TOOL_DENIED, task_id=task.id,
                                        tool_name=call.name))
                else:
                    t0 = time.monotonic()
                    output, is_error = self._tools.execute(call.name, call.arguments)
                    self._emit(RunEvent(
                        kind=events.TOOL_COMPLETED, task_id=task.id,
                        tool_name=call.name, tool_output=output, tool_error=is_error,
                        elapsed_seconds=time.monotonic() - t0,
                    ))
                    # 工具执行出错 -> 任务失败,交给 Replanner 决定重试/追加补救
                    if is_error:
                        return TaskOutcome(
                            status=FAILED,
                            error=f"工具 {call.name} 执行失败: {output}",
                            evidence=last_text.strip(),
                            text=last_text.strip(),
                            tool_calls_made=tool_calls_made,
                        )

                tool_results.append(ToolResult(
                    tool_call_id=call.id, output=output, is_error=is_error,
                ))

            messages.extend(self._llm.format_tool_results(tool_results))

            # 审批被拒:本任务阻塞,等用户介入,不继续往下试
            if denied_any:
                return TaskOutcome(
                    status=BLOCKED,
                    error="用户拒绝了所需工具的执行",
                    evidence=last_text.strip(),
                    text=last_text.strip(),
                    tool_calls_made=tool_calls_made,
                )

        # 步数耗尽仍未收口:标记失败,让 Replanner 决定是否追加任务继续
        return TaskOutcome(
            status=FAILED,
            error=f"任务在 {max_steps} 步内未完成",
            evidence=last_text.strip(),
            text=last_text.strip(),
            tool_calls_made=tool_calls_made,
        )
