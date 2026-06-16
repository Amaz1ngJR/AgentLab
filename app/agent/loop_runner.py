"""LoopRunner —— Loop Engineering 状态机。

驱动"执行→验证→诊断→修复→学习"的循环，直到验证通过、被阻塞、预算耗尽或用户停止。

状态机（PRD §7.6.3）：
  DraftGoal → Ready → Planning → Executing → Verifying
    → Succeeded (验证通过) → Learning
    → Diagnosing (失败/不确定) → Repairing → Executing
    → Blocked (权限不足/外部依赖)
    → BudgetExhausted (预算耗尽)
    → Cancelled (用户停止)

设计原则：
  - 复用现有 Planner/Executor/Replanner 作为 Task 层
  - 每个 iteration 记录任务、工具、验证证据、失败分类
  - 成功标准不能被模型静默降低（需用户确认）
  - 所有状态变化通过 RunEvent 通知 UI

PRD §7.6.4
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from app.agent.cancel import CancelToken
from app.agent.events import (
    GOAL_DEFINED,
    LOOP_BLOCKED,
    LOOP_BUDGET_EXHAUSTED,
    LOOP_COMPLETED,
    LOOP_ITERATION_STARTED,
    LOOP_STARTED,
    REPAIR_PLANNED,
    VERIFICATION_COMPLETED,
    VERIFICATION_STARTED,
    WORKTREE_PREPARED,
    RunEvent,
)
from app.agent.goals import GoalSpec
from app.agent.orchestrator import Orchestrator
from app.agent.planner import Planner
from app.agent.tasks import TaskStore
from app.agent.verifier import Verifier, VerificationResult
from app.workspace.worktree import WorktreeInfo, WorktreeManager


@dataclass
class LoopBudgetUsed:
    """Loop 已消耗预算。"""
    iterations: int = 0
    tool_calls: int = 0
    runtime_seconds: float = 0.0
    cost_usd: float = 0.0


class LoopRunner:
    """Loop Engineering 状态机。"""

    def __init__(
        self,
        goal: GoalSpec,
        orchestrator: Orchestrator,
        verifier: Verifier,
        worktree_manager: WorktreeManager | None = None,
        on_event: Callable[[RunEvent], None] | None = None,
    ):
        """
        Args:
            goal: GoalSpec 目标定义
            orchestrator: 复用的 Task 层编排器
            verifier: 验证器
            worktree_manager: Worktree 管理器（workspace_mode=git_worktree 时需要）
            on_event: 事件回调
        """
        self.goal = goal
        self.orchestrator = orchestrator
        self.verifier = verifier
        self.worktree_manager = worktree_manager
        self.on_event = on_event or (lambda e: None)

        self.loop_id = f"loop-{uuid.uuid4().hex[:8]}"
        self.status = "ready"
        self.current_iteration = 0
        self.budget_used = LoopBudgetUsed()
        self.worktree: WorktreeInfo | None = None
        self.verification_results: list[VerificationResult] = []
        self.started_at: float | None = None

    def run(self, cancel: CancelToken | None = None) -> str:
        """运行 Loop，返回最终状态消息。

        状态机流程：
          1. 准备 workspace（如需 worktree）
          2. 规划初始任务（复用 Planner）
          3. 循环：执行 → 验证 → 诊断/修复
          4. 结束：成功/阻塞/预算耗尽/取消

        Returns:
            最终状态描述文本
        """
        cancel = cancel or CancelToken()
        self.started_at = time.time()

        try:
            # 发送 loop_started 事件
            self.on_event(RunEvent(
                kind=LOOP_STARTED,
                text=f"开始 Loop: {self.goal.objective}",
                payload={
                    "loop_id": self.loop_id,
                    "goal_id": self.goal.goal_id,
                    "budgets": {
                        "max_iterations": self.goal.budgets.max_iterations,
                        "max_runtime_minutes": self.goal.budgets.max_runtime_minutes,
                        "max_tool_calls": self.goal.budgets.max_tool_calls,
                    },
                },
            ))

            # 1. 准备 workspace
            if self.goal.workspace_mode == "git_worktree":
                self._prepare_worktree()

            # 2. 规划初始任务（委托给 Planner，已在 orchestrator 中）
            self.status = "planning"

            # 3. 循环：执行 → 验证 → 诊断/修复
            while not cancel.cancelled:
                # 检查预算
                if self._is_budget_exhausted():
                    return self._finish_budget_exhausted()

                # 检查最大迭代次数
                if self.current_iteration >= self.goal.budgets.max_iterations:
                    return self._finish_budget_exhausted()

                # 开始新 iteration
                self.current_iteration += 1
                self.status = "executing"
                self.on_event(RunEvent(
                    kind=LOOP_ITERATION_STARTED,
                    text=f"Iteration {self.current_iteration}",
                    payload={"iteration": self.current_iteration},
                ))

                # 执行任务（委托给 Orchestrator）
                exec_result = self._execute_iteration(cancel)
                if exec_result == "blocked":
                    return self._finish_blocked("审批被拒或权限不足")
                elif exec_result == "cancelled":
                    return self._finish_cancelled()

                # 验证
                self.status = "verifying"
                verification = self._verify()
                self.verification_results.append(verification)

                # 判断验证结果
                if verification.is_success():
                    return self._finish_succeeded(verification)
                elif verification.status == "blocked":
                    return self._finish_blocked(verification.next_hint or "验证被阻塞")
                else:
                    # 失败或不确定 → 诊断并修复
                    self.status = "diagnosing"
                    self._diagnose_and_repair(verification)

            # 循环被取消
            return self._finish_cancelled()

        except Exception as exc:
            self.status = "failed"
            return f"Loop 异常终止: {exc}"

    def _prepare_worktree(self) -> None:
        """准备 worktree 隔离工作区。"""
        if not self.worktree_manager:
            raise ValueError("workspace_mode=git_worktree 需要 WorktreeManager")

        worktree_id = f"{self.goal.goal_id}-{self.loop_id}"
        self.worktree = self.worktree_manager.create(
            worktree_id=worktree_id,
            require_clean_base=False,  # 允许原工作区有改动
        )

        self.on_event(RunEvent(
            kind=WORKTREE_PREPARED,
            text=f"已创建隔离工作区: {self.worktree.path}",
            payload={
                "worktree_id": worktree_id,
                "path": str(self.worktree.path),
                "base_branch": self.worktree.base_branch,
                "base_commit": self.worktree.base_commit,
            },
        ))

    def _execute_iteration(self, cancel: CancelToken) -> str:
        """执行一轮任务。

        Returns:
            "ok" / "blocked" / "cancelled"
        """
        # 委托给 Orchestrator 执行任务
        # 这里简化处理：假设 orchestrator 已经绑定到正确的工作区
        # 实际实现需要根据 self.worktree 切换 cwd

        # TODO: 调用 orchestrator.run() 并处理结果
        # 暂时模拟执行
        time.sleep(0.1)  # 模拟执行
        self.budget_used.tool_calls += 5  # 模拟工具调用

        if cancel.cancelled:
            return "cancelled"

        return "ok"

    def _verify(self) -> VerificationResult:
        """运行验证计划。"""
        self.on_event(RunEvent(
            kind=VERIFICATION_STARTED,
            text=f"开始验证 (第 {self.current_iteration} 轮)",
        ))

        # 转换 GoalSpec.verification_plan 为 VerificationCheck
        from app.agent.goals import VerificationCheck
        checks = [
            VerificationCheck(**check_dict)
            for check_dict in self.goal.verification_plan
        ]

        # 运行验证
        result = self.verifier.verify(checks)

        self.on_event(RunEvent(
            kind=VERIFICATION_COMPLETED,
            text=f"验证{'通过' if result.is_success() else '失败'}",
            payload={
                "status": result.status,
                "checks": [
                    {"name": c.name, "status": c.status, "summary": c.summary}
                    for c in result.checks
                ],
            },
        ))

        return result

    def _diagnose_and_repair(self, verification: VerificationResult) -> None:
        """诊断失败原因并规划修复。"""
        self.status = "repairing"

        # 简化版：直接告诉 Replanner "验证失败，需要修复"
        # 实际实现需要根据 verification.failure_category 生成详细诊断
        repair_hint = verification.next_hint or "验证失败，需要修复"

        self.on_event(RunEvent(
            kind=REPAIR_PLANNED,
            text=f"规划修复: {repair_hint}",
            payload={"failure_category": verification.failure_category},
        ))

        # TODO: 调用 Replanner 追加修复任务
        # 这里简化处理：下一轮 iteration 会重新执行

    def _is_budget_exhausted(self) -> bool:
        """检查预算是否耗尽。"""
        if self.started_at:
            runtime = time.time() - self.started_at
            if runtime > self.goal.budgets.max_runtime_minutes * 60:
                return True

        if self.budget_used.tool_calls >= self.goal.budgets.max_tool_calls:
            return True

        if self.goal.budgets.max_cost_usd:
            if self.budget_used.cost_usd >= self.goal.budgets.max_cost_usd:
                return True

        return False

    def _finish_succeeded(self, verification: VerificationResult) -> str:
        """Loop 成功完成。"""
        self.status = "succeeded"

        # 生成 diff summary（如果有 worktree）
        diff_summary = ""
        if self.worktree and self.worktree_manager:
            diff_summary = self.worktree_manager.get_diff_summary(self.worktree)

        self.on_event(RunEvent(
            kind=LOOP_COMPLETED,
            text=f"目标达成！共 {self.current_iteration} 轮迭代",
            payload={
                "loop_id": self.loop_id,
                "iterations": self.current_iteration,
                "verification": {
                    "status": verification.status,
                    "checks": len(verification.checks),
                },
                "diff_summary": diff_summary[:500],  # 截断
            },
        ))

        msg = f"✓ 目标达成！\n验证通过，共 {self.current_iteration} 轮迭代。"
        if self.worktree:
            msg += f"\n\n改动已在隔离 worktree: {self.worktree.path}"
            if self.worktree_manager:
                merge_cmd = self.worktree_manager.merge_suggestion(self.worktree)
                msg += f"\n\n{merge_cmd}"
        return msg

    def _finish_blocked(self, reason: str) -> str:
        """Loop 被阻塞。"""
        self.status = "blocked"
        self.on_event(RunEvent(
            kind=LOOP_BLOCKED,
            text=f"Loop 被阻塞: {reason}",
            payload={"reason": reason},
        ))
        return f"⊘ Loop 被阻塞: {reason}"

    def _finish_budget_exhausted(self) -> str:
        """预算耗尽。"""
        self.status = "budget_exhausted"
        self.on_event(RunEvent(
            kind=LOOP_BUDGET_EXHAUSTED,
            text=f"预算耗尽 (已执行 {self.current_iteration} 轮)",
            payload={
                "iterations": self.current_iteration,
                "budget_used": {
                    "iterations": self.budget_used.iterations,
                    "tool_calls": self.budget_used.tool_calls,
                    "runtime_seconds": self.budget_used.runtime_seconds,
                },
            },
        ))
        return f"预算耗尽。已执行 {self.current_iteration} 轮，未达成目标。"

    def _finish_cancelled(self) -> str:
        """用户取消。"""
        self.status = "cancelled"
        return f"Loop 已取消 (已执行 {self.current_iteration} 轮)"
