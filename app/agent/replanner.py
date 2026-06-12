"""Replanner —— 根据 Executor 的执行结果调整 TaskStore。

职责(technical_architecture.md §7.1):
  根据执行结果、错误、用户拒绝、环境变化调整任务、追加任务或标记阻塞。

当前实现是启发式(非 LLM),规则简单可测;后续可替换成"让模型看 outcome 再
产出 plan patch"。规则:

  completed -> 任务置 completed,evidence 落库。
  blocked(审批被拒) -> 任务置 blocked,记录原因,不追加任务(等用户介入)。
  failed:
    - 首次失败:置 failed,并追加一个"复查/换方案"补救任务(依赖原任务),
      给编排路径一次自我修复机会。
    - 已是重试任务再失败(content 带补救标记):只置 failed,不再追加,避免
      无限追加导致 run 永不收敛。

TaskStore 是唯一可信状态源,所有回写都通过它,Replanner 不持有独立任务副本。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.executor import TaskOutcome
from app.agent.tasks import (
    BLOCKED,
    COMPLETED,
    FAILED,
    PENDING,
    Task,
    TaskStore,
)

# 补救任务 content 的前缀标记:用来识别"这是个重试任务",避免重试再失败时无限追加
_RETRY_PREFIX = "复查并修复:"


@dataclass
class PlanPatch:
    """一次重规划的结果摘要,便于审计/事件展示。

    task_id     - 被处理的任务。
    new_status  - 该任务被置成的状态。
    added       - 本次新追加的任务(id 列表)。
    note        - 一句话说明。
    """
    task_id: str
    new_status: str
    added: list[str] = field(default_factory=list)
    note: str = ""


class Replanner:
    """把 TaskOutcome 落到 TaskStore,并按需追加补救任务。"""

    def __init__(self, store: TaskStore):
        self._store = store
        self._retry_seq = 0  # 追加补救任务的自增计数,保证 id 唯一

    def apply(self, task: Task, outcome: TaskOutcome) -> PlanPatch:
        """根据 outcome 回写 task 状态,必要时追加任务。返回 PlanPatch。"""
        if outcome.status == COMPLETED:
            self._store.update_status(
                task.id, COMPLETED,
                evidence=outcome.evidence, error="",
                note="completed",
            )
            return PlanPatch(task_id=task.id, new_status=COMPLETED, note="任务完成")

        if outcome.status == BLOCKED:
            self._store.update_status(
                task.id, BLOCKED,
                evidence=outcome.evidence, error=outcome.error,
                note=f"blocked: {outcome.error}",
            )
            return PlanPatch(task_id=task.id, new_status=BLOCKED,
                             note="任务阻塞(等待用户)")

        # 其余按 failed 处理
        self._store.update_status(
            task.id, FAILED,
            evidence=outcome.evidence, error=outcome.error,
            note=f"failed: {outcome.error}",
        )

        # 已是重试任务则不再追加,避免无限循环
        is_retry = task.content.startswith(_RETRY_PREFIX)
        if is_retry:
            return PlanPatch(task_id=task.id, new_status=FAILED,
                             note="重试任务再次失败,停止追加")

        self._retry_seq += 1
        retry = Task(
            id=f"{task.id}-retry{self._retry_seq}",
            content=f"{_RETRY_PREFIX}{task.content}(原因: {outcome.error})",
            status=PENDING,
            dependencies=[],  # 原任务已 failed,补救任务不能依赖它(否则永远卡住)
        )
        self._store.add(retry)
        return PlanPatch(task_id=task.id, new_status=FAILED, added=[retry.id],
                         note=f"失败,追加补救任务 {retry.id}")
