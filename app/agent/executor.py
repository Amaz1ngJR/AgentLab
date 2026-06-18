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
from app.agent.cancel import CancelToken, Cancelled
from app.agent.events import RunEvent
from app.agent.tasks import BLOCKED, COMPLETED, FAILED, Task
from app.models.protocol import ToolResult
from app.models.router import ModelRouter
from app.tools.registry import ToolRegistry

DENIED_MESSAGE = (
    "User denied execution of this tool. Do not retry without first confirming with the user."
)

# 取消 / 早返回时,给"模型已请求但未执行"的工具补的合成结果。Anthropic 要求每个
# tool_use 都必须有配对的 tool_result,否则下一轮请求会因悬空 tool_call 报错。
# 中断后用户往往要重新引导(steering),历史必须处于可继续状态,所以这里兜底补齐。
INTERRUPTED_TOOL_RESULT = "[用户已中断,此工具未执行]"

# 工具输出进入对话历史前的截断上限(字符数)。超大输出(读大文件、长 shell stdout)
# 若原样塞进 messages,会一条消息就撑爆上下文窗口,且压缩也压不动(它落在最近窗口
# 受保护区)。参考 Claude Code / Aider 的做法:在工具结果进入历史时就截断,保留头尾
# 两端(头部含主要内容,尾部常含结论/报错),中间用标记说明省略量,让模型知道被截断。
TOOL_OUTPUT_HEAD_CHARS = 8_000   # 保留开头多少字符
TOOL_OUTPUT_TAIL_CHARS = 2_000   # 保留结尾多少字符
TOOL_OUTPUT_MAX_CHARS = TOOL_OUTPUT_HEAD_CHARS + TOOL_OUTPUT_TAIL_CHARS  # 超过才截断


def _truncate_tool_output(output: str, max_chars: int = TOOL_OUTPUT_MAX_CHARS) -> str:
    """把过长的工具输出截断成"头部 + 省略标记 + 尾部",防止单条结果撑爆上下文。

    不超过 max_chars 原样返回;超过则保留前 TOOL_OUTPUT_HEAD_CHARS 字符与后
    TOOL_OUTPUT_TAIL_CHARS 字符,中间插一行标记说明省略了多少字符。这样模型既能
    看到主要内容和结尾(报错/结论常在末尾),也明确知道中间有内容被省略。
    """
    if not output or len(output) <= max_chars:
        return output
    head = output[:TOOL_OUTPUT_HEAD_CHARS]
    tail = output[-TOOL_OUTPUT_TAIL_CHARS:]
    omitted = len(output) - TOOL_OUTPUT_HEAD_CHARS - TOOL_OUTPUT_TAIL_CHARS
    return (
        f"{head}\n"
        f"\n[... 工具输出过长,已省略中间 {omitted} 个字符(共 {len(output)} 字符);"
        f"如需完整内容请用更精确的参数重新调用 ...]\n\n"
        f"{tail}"
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
            # 记录本轮已产出结果的 tool_call id;取消/早返回时据此给剩余的补合成结果,
            # 保证 assistant 里每个 tool_use 都有配对 tool_result(否则下一轮 provider 报错)。
            resulted_ids: set[str] = set()

            def _flush_pending_tool_results() -> None:
                """给本轮 resp.tool_calls 里还没结果的工具补合成 tool_result 并入历史。"""
                pending = [
                    ToolResult(tool_call_id=c.id, output=INTERRUPTED_TOOL_RESULT,
                               is_error=False)
                    for c in resp.tool_calls if c.id not in resulted_ids
                ]
                if pending:
                    messages.extend(self._llm.format_tool_results(tool_results + pending))
                    tool_results.clear()  # 已随 pending 一起入历史,避免下方重复 extend

            try:
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
                        # 进入对话历史前截断超大输出(根治"单条大结果撑爆窗口")。
                        # 截断后再发事件 / 入 messages,保证历史里的副本是有界的。
                        output = _truncate_tool_output(output)
                        self._emit(RunEvent(
                            kind=events.TOOL_COMPLETED, task_id=task.id,
                            tool_name=call.name, tool_output=output, tool_error=is_error,
                            elapsed_seconds=time.monotonic() - t0,
                        ))
                        # 工具执行出错 -> 任务失败。先补齐 tool_result 配对,再返回,
                        # 否则 assistant 的 tool_use 悬空,Replanner 重试时 provider 报错。
                        if is_error:
                            tool_results.append(ToolResult(
                                tool_call_id=call.id, output=output, is_error=is_error,
                            ))
                            resulted_ids.add(call.id)
                            _flush_pending_tool_results()
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
                    resulted_ids.add(call.id)
            except Cancelled:
                # 取消:给剩余未执行工具补合成结果,保证配对完整,历史可继续(供 steering)。
                _flush_pending_tool_results()
                raise

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
