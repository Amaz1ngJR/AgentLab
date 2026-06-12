"""结构化 RunEvent —— 编排路径(Planner/Executor/Replanner)对外的统一事件协议。

为什么不复用 runtime.TurnEvent:
  TurnEvent 是"单轮工具循环"的事件(text / tool_call / tool_result / tool_denied),
  没有任务、计划、run 生命周期的概念。编排路径需要把"计划生成、按依赖执行、
  重规划、阻塞、run 完成/失败"也表达出来,且要让 CLI 和未来的 Web UI / TUI 用
  同一套事件渲染。所以这里定义更完整的 RunEvent。

事件种类(kind):
  run_started        一次 run 开始(携带 goal)
  plan_created       Planner 产出初始计划(payload.tasks = snapshot)
  task_started       Executor claim 了某个任务(task_id / task_content)
  message_delta      模型流式文本增量(text)
  tool_requested     模型请求调用工具(tool_name / tool_input)
  approval_required  工具需要审批(tool_name / tool_input);审批结果体现在后续事件
  tool_completed     工具执行完成(tool_name / tool_output / tool_error / elapsed)
  tool_denied        审批被拒(tool_name)
  task_updated       任务状态变化(task_id / task_status / 携带 snapshot)
  run_completed      run 正常结束(text = 最终答复,payload.tasks = 最终 snapshot)
  run_failed         run 异常/卡死/取消结束(text = 原因)

  上下文预算与压缩(§7.3,由 ContextManager 在稳定点发出):
  context_budget_warning      预计输入超过窗口的 70%(payload 携带预算明细)
  context_compaction_started  开始压缩旧历史(payload.token_before)
  context_compaction_completed 压缩完成(payload: token_before/after/range/summary)
  context_compaction_failed   压缩失败,保留原始尾部(text = 原因)

设计原则:RunEvent 是只读的事实通知,消费者(CLI/Web/TUI)只渲染,不回写状态。
审批的"询问—回答"仍由 ApprovalPolicy 同步完成;approval_required 只是给 UI 一个
展示时机。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── kind 常量 ─────────────────────────────────────────────────────────────────
RUN_STARTED = "run_started"
PLAN_CREATED = "plan_created"
TASK_STARTED = "task_started"
MESSAGE_DELTA = "message_delta"
TOOL_REQUESTED = "tool_requested"
APPROVAL_REQUIRED = "approval_required"
TOOL_COMPLETED = "tool_completed"
TOOL_DENIED = "tool_denied"
TASK_UPDATED = "task_updated"
RUN_COMPLETED = "run_completed"
RUN_FAILED = "run_failed"

# 上下文预算与压缩(§7.3)
CONTEXT_BUDGET_WARNING = "context_budget_warning"
CONTEXT_COMPACTION_STARTED = "context_compaction_started"
CONTEXT_COMPACTION_COMPLETED = "context_compaction_completed"
CONTEXT_COMPACTION_FAILED = "context_compaction_failed"


@dataclass
class RunEvent:
    """编排路径产生的单个事件,通过 on_event 回调推给 UI 层。

    字段按 kind 取用,未用到的留默认值。payload 放结构化附加数据
    (例如 plan_created / task_updated / run_completed 携带 {"tasks": snapshot})。
    """
    kind: str
    text: str = ""

    # 任务相关
    task_id: str = ""
    task_content: str = ""
    task_status: str = ""

    # 工具相关
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_output: str = ""
    tool_error: bool = False
    elapsed_seconds: float = 0.0

    # 结构化附加数据(如任务 snapshot、token 用量)
    payload: dict[str, Any] = field(default_factory=dict)
