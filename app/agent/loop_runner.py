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

from app.agent.approval import ApprovalPolicy
from app.agent.cancel import CancelToken
from app.agent.events import (
    GOAL_DEFINED,
    LOOP_BLOCKED,
    LOOP_BUDGET_EXHAUSTED,
    LOOP_COMPLETED,
    LOOP_FAILED,
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
from app.config.loader import use_workspace_root
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
        approval: ApprovalPolicy | None = None,
        on_event: Callable[[RunEvent], None] | None = None,
    ):
        """
        Args:
            goal: GoalSpec 目标定义
            orchestrator: 复用的 Task 层编排器
            verifier: 验证器
            worktree_manager: Worktree 管理器（workspace_mode=git_worktree 时需要）
            approval: worktree 提交等高风险收尾动作的审批策略
            on_event: 事件回调
        """
        self.goal = goal
        self.orchestrator = orchestrator
        self.verifier = verifier
        self.worktree_manager = worktree_manager
        self.approval = approval
        self.on_event = on_event or (lambda e: None)

        self.loop_id = f"loop-{uuid.uuid4().hex[:8]}"
        self.status = "ready"
        self.current_iteration = 0
        self.budget_used = LoopBudgetUsed()
        self.worktree: WorktreeInfo | None = None
        self.verification_results: list[VerificationResult] = []
        self.started_at: float | None = None
        # 下一轮要执行的指令。第一轮是 None(用 objective 构建初始指令);
        # 验证失败后 _diagnose_and_repair 会填入修复指令,下一轮带 resume=True 执行。
        self._next_instruction: str | None = None
        self._last_execution_error: str | None = None

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
                self.budget_used.iterations = self.current_iteration
                if self.started_at:
                    self.budget_used.runtime_seconds = time.time() - self.started_at
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
                elif exec_result == "failed":
                    return self._finish_failed(
                        self._last_execution_error or "执行阶段发生未知异常"
                    )

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
            return self._finish_failed(f"{type(exc).__name__}: {exc}")

    def _prepare_worktree(self) -> None:
        """准备 worktree 隔离工作区。"""
        if not self.worktree_manager:
            raise ValueError("workspace_mode=git_worktree 需要 WorktreeManager")

        worktree_id = f"{self.goal.goal_id}-{self.loop_id}"
        self.worktree = self.worktree_manager.create(
            worktree_id=worktree_id,
            require_clean_base=False,  # 允许原工作区有改动
        )
        # 验证器也要在 worktree 内跑命令/查文件,否则会去主工作区验证(看不到改动)。
        self.verifier.workspace_root = self.worktree.path

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

    def _build_initial_instruction(self) -> str:
        """把 GoalSpec 拼成给 Orchestrator 的首轮执行指令。"""
        lines = [self.goal.objective, ""]
        if self.goal.success_criteria:
            lines.append("成功标准(必须全部满足):")
            lines.extend(f"  - {c}" for c in self.goal.success_criteria)
        constraints = self.goal.constraints or {}
        if constraints:
            lines.append("")
            lines.append("约束:")
            for key, vals in constraints.items():
                if vals:
                    lines.append(f"  - {key}: {', '.join(vals)}")
        return "\n".join(lines)

    def _execute_iteration(self, cancel: CancelToken) -> str:
        """执行一轮任务,委托给 Orchestrator。

        - 第一轮:用 objective + success_criteria 构建初始指令,resume=False。
        - 修复轮:用 _diagnose_and_repair 填好的修复指令,resume=True(保留已有任务,
          失败任务重置为 pending 继续推进)。
        - 若有 worktree,用 use_workspace_root 把文件/shell 工具的根目录切到 worktree,
          确保改动落在隔离区。
        - 累计真实工具调用数到预算;按 Orchestrator.last_run_status 判 blocked/cancelled。

        Returns:
            "ok" / "blocked" / "cancelled" / "failed"
        """
        if self._next_instruction is None:
            instruction = self._build_initial_instruction()
            resume = False
        else:
            instruction = self._next_instruction
            resume = True
        self._next_instruction = None  # 用过即清,下一轮默认重新走验证驱动

        def _do_run() -> str:
            return self.orchestrator.run(instruction, cancel=cancel, resume=resume)

        try:
            if self.worktree is not None:
                with use_workspace_root(self.worktree.path):
                    _do_run()
            else:
                _do_run()
        except Exception as exc:
            # 执行器异常不能继续交给普通 verifier。否则已有文件/旧测试可能碰巧
            # 通过，导致 Loop 把“执行崩溃”误报为“目标达成”。
            self._last_execution_error = f"{type(exc).__name__}: {exc}"
            self.budget_used.tool_calls += getattr(
                self.orchestrator, "last_run_tool_calls", 0)
            return "failed"

        # 累计真实工具调用数(替掉原来写死的 +5)
        self.budget_used.tool_calls += getattr(
            self.orchestrator, "last_run_tool_calls", 0)

        run_status = getattr(self.orchestrator, "last_run_status", "")
        if cancel.cancelled or run_status == "cancelled":
            return "cancelled"
        if run_status == "blocked":
            return "blocked"
        # completed / failed 都进入验证:验证才是 Loop 是否达成目标的唯一裁判。
        return "ok"

    def _verify(self) -> VerificationResult:
        """运行验证计划。"""
        self.on_event(RunEvent(
            kind=VERIFICATION_STARTED,
            text=f"开始验证 (第 {self.current_iteration} 轮)",
        ))

        # GoalSpec.verification_plan 已是 VerificationCheck 列表;若是从存储恢复的
        # dict(向后兼容),就地转成 VerificationCheck。
        from app.agent.goals import VerificationCheck
        checks = [
            c if isinstance(c, VerificationCheck) else VerificationCheck(**c)
            for c in self.goal.verification_plan
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
        """诊断失败原因,把修复指令排进下一轮。

        把验证失败的具体检查项 + 失败摘要拼成修复指令,存进 _next_instruction。
        下一轮 _execute_iteration 会用 resume=True 调 Orchestrator —— 保留已有任务、
        把上一轮 failed 的任务重置为 pending,并在其上追加这条修复计划继续推进。
        """
        self.status = "repairing"

        # 收集失败/阻塞的检查项,组成诊断说明
        failed_checks = [
            c for c in verification.checks
            if c.status in ("fail", "blocked", "uncertain")
        ]
        detail_lines = []
        for c in failed_checks:
            line = f"  - [{c.status}] {c.name}: {c.summary}"
            if c.error:
                line += f"\n    错误: {c.error[:300]}"
            detail_lines.append(line)
        detail = "\n".join(detail_lines) or (verification.next_hint or "验证未通过")

        repair_instruction = (
            f"上一轮改动未通过验证(失败分类: {verification.failure_category or '未知'})。\n"
            f"验证失败详情:\n{detail}\n\n"
            f"请诊断根因并修复,使下列成功标准全部满足:\n"
            + "\n".join(f"  - {c}" for c in self.goal.success_criteria)
        )
        self._next_instruction = repair_instruction

        self.on_event(RunEvent(
            kind=REPAIR_PLANNED,
            text=f"规划修复: {verification.next_hint or '验证失败'}",
            payload={
                "failure_category": verification.failure_category,
                "failed_checks": [c.name for c in failed_checks],
            },
        ))

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
        commit_sha = ""
        commit_error = ""
        if self.worktree and self.worktree_manager:
            diff_summary = self.worktree_manager.get_diff_summary(self.worktree)
            if self.worktree_manager.check_dirty(self.worktree):
                approval_args = {
                    "summary": diff_summary[:1000],
                    "worktree": str(self.worktree.path),
                    "branch": f"worktree/{self.worktree.worktree_id}",
                }
                try:
                    approved = (
                        self.approval is not None
                        and self.approval.request("worktree_commit", approval_args)
                    )
                except Exception as exc:
                    approved = False
                    commit_error = f"无法请求 worktree 提交审批: {exc}"
                if approved:
                    try:
                        commit_sha = self.worktree_manager.commit_all(
                            self.worktree,
                            f"AgentLab loop {self.goal.goal_id}: verified changes",
                        )
                    except Exception as exc:
                        commit_error = str(exc)

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
                "commit_sha": commit_sha,
                "commit_error": commit_error,
            },
        ))

        msg = f"✓ 目标达成！\n验证通过，共 {self.current_iteration} 轮迭代。"
        if self.worktree:
            msg += f"\n\n改动已在隔离 worktree: {self.worktree.path}"
            if self.worktree_manager:
                if commit_sha:
                    msg += f"\n已生成验证提交: {commit_sha[:12]}"
                elif commit_error:
                    msg += f"\n自动提交失败，改动仍保留在 worktree: {commit_error}"
                elif self.worktree_manager.check_dirty(self.worktree):
                    msg += "\n用户未批准生成提交，改动仍保留在 worktree。"
                merge_cmd = self.worktree_manager.merge_suggestion(self.worktree)
                msg += f"\n\n{merge_cmd}"
        return msg

    def _finish_failed(self, reason: str) -> str:
        """Loop 因内部执行异常失败，禁止继续验证并误报成功。"""
        self.status = "failed"
        self.on_event(RunEvent(
            kind=LOOP_FAILED,
            text=f"Loop 执行失败: {reason}",
            payload={"reason": reason},
        ))
        return f"Loop 执行失败: {reason}"

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
