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
from app.agent.approval import ApprovalPolicy, AutoApprove, request_tool_approval
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


def _looks_like_action_task(task_content: str) -> bool:
    """判断任务描述是否明显需要工具操作（而不是纯思考/总结）。

    用于空转检测：如果任务看起来需要工具但模型没调用，给予提示。
    """
    content_lower = task_content.lower()
    # 包含这些关键词的任务通常需要工具操作
    action_keywords = [
        "读", "read", "查看", "打开", "文件",
        "写", "write", "修改", "编辑", "edit", "改",
        "执行", "运行", "run", "shell", "命令",
        "搜索", "search", "查找", "grep", "find",
        "创建", "create", "新建", "删除", "delete",
        "测试", "test", "验证", "check",
    ]
    # 包含这些关键词的任务通常是纯思考，不需要工具
    thinking_keywords = [
        "分析", "总结", "解释", "说明", "描述",
        "思考", "判断", "评估", "建议",
    ]
    has_action = any(kw in content_lower for kw in action_keywords)
    only_thinking = all(kw not in content_lower for kw in action_keywords) and \
                    any(kw in content_lower for kw in thinking_keywords)
    return has_action and not only_thinking


def _looks_like_completion_message(text: str) -> bool:
    """判断模型输出是否像"任务已完成/无需操作"的消息。

    用于空转检测的例外：有些任务检查后发现不需要操作，模型直接说明即可。
    """
    if not text:
        return False
    text_lower = text.lower()
    completion_patterns = [
        "已完成", "完成", "done", "finished", "completed",
        "无需", "不需要", "no need", "not needed", "unnecessary",
        "已经", "already",
        "成功", "successfully", "success",
    ]
    return any(pattern in text_lower for pattern in completion_patterns)

# progress 工厂签名:接收 label,返回一个上下文管理器(可带 .update / .on_text)
ProgressFn = Callable[[str], ContextManager[Any]]

# 注入到 messages 的任务指令模板：工具仅用于确实需要外部操作的任务，
# 纯对话、解释、总结等任务应直接回答，避免模型为了满足形式要求反复调用无意义工具。
_TASK_DIRECTIVE = (
    "【当前子任务】{content}\n\n"
    "执行要求:\n"
    "- 先判断任务是否确实需要读取文件、修改代码、执行命令或联网查询。\n"
    "- 需要外部操作时，立即调用最相关的工具，不要只描述计划。\n"
    "- 纯对话、介绍、解释、总结或无需外部信息的问题，直接回答，不要调用工具。\n"
    "- 完成后直接给出结果，避免重复调用工具或重复回答。"
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
    model_rounds: int = 0


class Executor:
    """按任务目标驱动有限步工具循环。"""

    def __init__(
        self,
        llm: ModelRouter,
        tools: ToolRegistry,
        approval: Optional[ApprovalPolicy] = None,
        on_event: Optional[Callable[[RunEvent], None]] = None,
        progress: Optional[ProgressFn] = None,
        context_manager: Optional[Any] = None,
    ):
        self._llm = llm
        self._tools = tools
        self._approval: ApprovalPolicy = approval or AutoApprove()
        self._emit = on_event or (lambda e: None)
        self._progress: ProgressFn = progress or (lambda label: nullcontext())
        self._ctx = context_manager  # 任务内压缩用

    def _tools_for_task(self, task: str, *, mode: str = "task") -> list[dict[str, Any]]:
        """按任务文本为 Executor 提供动态工具 Schema。"""
        selector = getattr(self._tools, "schemas_for_task", None)
        if callable(selector):
            return selector(task, mode=mode)
        return self._tools.schemas()

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
        model_rounds = 0
        no_progress_rounds = 0
        seen_tool_signatures: set[tuple[str, str]] = set()

        for _ in range(max(1, max_steps)):
            model_rounds += 1
            if cancel is not None:
                cancel.raise_if_cancelled()

            tools = self._tools_for_task(task.content, mode="task") or None
            text_streamed = False
            with self._progress("thinking") as handle:
                on_progress = getattr(handle, "update", None)
                raw_on_text = getattr(handle, "on_text", None)
                raw_on_thinking = getattr(handle, "on_thinking", None)

                if self._ctx is not None:
                    try:
                        self._ctx.compact_before_model_call(
                            messages,
                            system=system,
                            tools=tools,
                            on_progress=on_progress,
                        )
                    except Exception:
                        pass

                def on_text_delta(delta: str) -> None:
                    nonlocal text_streamed
                    text_streamed = True
                    if raw_on_text is not None:
                        raw_on_text(delta)

                resp = self._llm.create_message(
                    messages=messages,
                    tools=tools,
                    system=system,
                    on_progress=on_progress,
                    on_text_delta=on_text_delta if raw_on_text else None,
                    on_thinking_delta=raw_on_thinking,
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
                # 模型输出新的完成文本，说明本轮有进展；只有首次空转才给一次纠正机会。
                no_progress_rounds = 0 if resp.text.strip() else no_progress_rounds + 1
                # 空转检测：第一轮就没调用工具，且任务描述明显需要工具操作时，给予提示
                # 但如果模型给出的文本说明了不需要操作（如"已完成"、"无需修改"等），则接受
                if tool_calls_made == 0 and _looks_like_action_task(task.content) and \
                   not _looks_like_completion_message(last_text):
                    # 这是第一轮，任务看起来需要工具操作，模型只输出了文字且不像完成消息
                    # 给模型一次纠正机会，明确提示必须调用工具
                    messages.append({
                        "role": "user",
                        "content": (
                            "注意：你只输出了文字说明，但没有调用任何工具。\n"
                            "请立即调用相关工具完成任务，而不是只描述要做什么。\n"
                            "例如：\n"
                            "- 需要读文件 → 调用 read_file\n"
                            "- 需要写文件 → 调用 write_file 或 edit_file\n"
                            "- 需要执行命令 → 调用 shell\n"
                            "- 需要搜索代码 → 调用 code_search\n\n"
                            "现在请立即调用工具完成任务。"
                        )
                    })
                    continue  # 给模型一次重新响应的机会
                # 否则认为任务确实完成了
                return TaskOutcome(
                    status=COMPLETED,
                    evidence=last_text.strip(),
                    text=last_text.strip(),
                    tool_calls_made=tool_calls_made,
                    model_rounds=model_rounds,
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
                    signature = (call.name, repr(sorted(call.arguments.items())))
                    if signature in seen_tool_signatures:
                        return TaskOutcome(
                            status=FAILED,
                            error=f"模型重复请求工具 {call.name}，任务无进展",
                            evidence=last_text.strip(),
                            text=last_text.strip(),
                            tool_calls_made=tool_calls_made,
                            model_rounds=model_rounds,
                        )
                    seen_tool_signatures.add(signature)
                    self._emit(RunEvent(kind=events.TOOL_REQUESTED, task_id=task.id,
                                        tool_name=call.name, tool_input=call.arguments))

                    tool = self._tools.get(call.name)
                    approval_action = (
                        tool.approval_action(call.arguments) if tool else None
                    )
                    needs_approval = approval_action is not None
                    if needs_approval:
                        self._emit(RunEvent(kind=events.APPROVAL_REQUIRED, task_id=task.id,
                                            tool_name=call.name, tool_input=call.arguments,
                                            payload={"approval_action": approval_action}))

                    approval_result = None
                    if needs_approval and tool is not None:
                        approval_result = request_tool_approval(
                            self._approval,
                            tool,
                            approval_action,
                            call.arguments,
                        )

                    if approval_result and not approval_result.approved:
                        # 用户拒绝或按 ESC 取消
                        if approval_result.feedback:
                            output = f"[denied] 用户拒绝并提供了修改建议:\n\n{approval_result.feedback}"
                        else:
                            output = DENIED_MESSAGE
                        is_error = False
                        denied_any = True
                        self._tools.record_denied(
                            call.name,
                            call.arguments,
                            approval_action=approval_action,
                        )
                        self._emit(RunEvent(kind=events.TOOL_DENIED, task_id=task.id,
                                            tool_name=call.name))

                        # 修改建议和 Esc 都意味着“停止当前 turn，回到主输入框”。先补齐
                        # tool_result 配对，避免历史损坏，再用协作式 Cancelled 收尾。
                        if approval_result.cancelled:
                            tool_results.append(ToolResult(
                                tool_call_id=call.id, output=output, is_error=False,
                            ))
                            resulted_ids.add(call.id)
                            _flush_pending_tool_results()
                            if approval_result.feedback:
                                raise Cancelled(approval_result.feedback)
                            raise KeyboardInterrupt("用户取消操作")
                    else:
                        t0 = time.monotonic()
                        output, is_error = self._tools.execute(
                            call.name,
                            call.arguments,
                            approved_action=approval_action,
                        )
                        # 进入对话历史前截断超大输出(根治"单条大结果撑爆窗口")。
                        # 截断后再发事件 / 入 messages,保证历史里的副本是有界的。
                        output = _truncate_tool_output(output)
                        self._emit(RunEvent(
                            kind=events.TOOL_COMPLETED, task_id=task.id,
                            tool_name=call.name, tool_input=call.arguments,
                            tool_output=output, tool_error=is_error,
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
                                model_rounds=model_rounds,
                            )

                    tool_results.append(ToolResult(
                        tool_call_id=call.id, output=output, is_error=is_error,
                    ))
                    resulted_ids.add(call.id)
            except BaseException:
                # Cancelled / KeyboardInterrupt / 任何其他异常:
                # 必须给本轮所有未执行的工具补合成 tool_result,保证 tool_use/tool_result
                # 配对完整。否则 messages 里留下悬空 tool_use,下一轮 API 调用会因
                # TOOL_USE_RESULT_MISMATCH 报错,且每轮都会持续失败(内存损坏即成立,
                # 无需持久化)。原先只捕获 Cancelled,双击 Ctrl-C(KeyboardInterrupt)
                # 或工具/回调的意外异常会直接逃逸,导致高频复现。
                _flush_pending_tool_results()
                raise

            messages.extend(self._llm.format_tool_results(tool_results))

            # tool_result 刚入历史后再次预检，尽早压缩下一轮要发送的上下文。
            if self._ctx is not None:
                try:
                    self._ctx.compact_before_model_call(
                        messages, system=system, tools=tools,
                        on_progress=on_progress,
                    )
                except Exception:
                    pass  # 压缩失败不能中断任务主路径

            # 审批被拒:本任务阻塞,等用户介入,不继续往下试
            if denied_any:
                return TaskOutcome(
                    status=BLOCKED,
                    error="用户拒绝了所需工具的执行",
                    evidence=last_text.strip(),
                    text=last_text.strip(),
                    tool_calls_made=tool_calls_made,
                    model_rounds=model_rounds,
                )

        # 步数耗尽仍未收口:标记失败,让 Replanner 决定是否追加任务继续
        return TaskOutcome(
            status=FAILED,
            error=f"任务在 {max_steps} 步内未完成",
            evidence=last_text.strip(),
            text=last_text.strip(),
            tool_calls_made=tool_calls_made,
            model_rounds=model_rounds,
        )
