"""Loop Engineering CLI 命令处理。

实现 /goal 和 /loop 命令族。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from app.agent.goals import GoalSpec
    from app.agent.loop_runner import LoopRunner
    from app.storage import Storage


def _goal_to_dict(goal: GoalSpec) -> dict:
    """GoalSpec → 可序列化字典(供 storage 存储)。"""
    from dataclasses import asdict
    return asdict(goal)


def _dict_to_goal(d: dict) -> GoalSpec:
    """存储字典 → GoalSpec(重建 dataclass 对象)。"""
    from app.agent.goals import GoalSpec, VerificationCheck, GoalBudgets
    # verification_plan 和 budgets 需要重建内嵌对象
    checks = [
        VerificationCheck(**c) if isinstance(c, dict) else c
        for c in d.get("verification_plan", [])
    ]
    budgets_data = d.get("budgets", {})
    budgets = GoalBudgets(**budgets_data) if isinstance(budgets_data, dict) else budgets_data
    return GoalSpec(
        goal_id=d["goal_id"],
        objective=d["objective"],
        success_criteria=d["success_criteria"],
        verification_plan=checks,
        constraints=d.get("constraints", {}),
        budgets=budgets,
        stop_conditions=d.get("stop_conditions", []),
        workspace_mode=d.get("workspace_mode", "git_worktree"),
        learning_policy=d.get("learning_policy", {}),
        session_id=d.get("session_id"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
    )


class LoopCommandHandler:
    """处理 /goal 和 /loop 命令。"""

    def __init__(
        self,
        storage: Storage,
        get_session: Callable,
        run_loop_fn: Callable[[LoopRunner], str],
        workspace_root: Path,
    ):
        """
        Args:
            storage: SQLite 存储(用于保存/加载 GoalSpec)
            get_session: 获取当前 AgentSession 的回调(返回 session 或 None)
            run_loop_fn: 执行 loop.run() 的回调(CLI 提供,带 Ctrl-C 取消处理)
            workspace_root: 当前工作区根目录(用于 Verifier / WorktreeManager)
        """
        self.storage = storage
        self.get_session = get_session
        self.run_loop_fn = run_loop_fn
        self.workspace_root = workspace_root
        self.current_goal: GoalSpec | None = None
        self.current_loop: LoopRunner | None = None

    def handle_goal_command(self, line: str) -> str | None:
        """处理 /goal 命令。

        支持：
          /goal                           # 显示当前 GoalSpec
          /goal new <objective> [:: <cmd>] # 创建 goal,可选指定验证命令
          /goal show <goal_id>            # 显示指定 goal
        """
        parts = line.strip().split(maxsplit=2)
        if len(parts) == 1:
            # /goal - 显示当前
            return self._show_current_goal()

        subcommand = parts[1]

        if subcommand == "new":
            rest = parts[2] if len(parts) > 2 else ""
            return self._create_goal_simple(rest)
        elif subcommand == "show":
            goal_id = parts[2] if len(parts) > 2 else None
            if not goal_id:
                return "用法: /goal show <goal_id>"
            return self._show_goal(goal_id)
        else:
            return (
                "未知子命令。支持:\n"
                "  /goal                - 显示当前 goal\n"
                "  /goal new <目标> [:: <验证命令>] - 创建新 goal\n"
                "  /goal show <id>      - 显示指定 goal"
            )

    def handle_loop_command(self, line: str) -> str | None:
        """处理 /loop 命令。

        支持：
          /loop start [goal_id]    # 启动 loop
          /loop status             # 显示状态
          /loop stop               # 停止（Ctrl-C）
        """
        parts = line.strip().split(maxsplit=2)
        if len(parts) == 1:
            return "用法: /loop start [goal_id] | /loop status | /loop stop"

        subcommand = parts[1]

        if subcommand == "start":
            goal_id = parts[2] if len(parts) > 2 else None
            return self._start_loop(goal_id)
        elif subcommand == "status":
            return self._show_loop_status()
        elif subcommand == "stop":
            return "Loop 停止功能:运行时按 Ctrl-C 或 Esc 中断。"
        else:
            return "未知子命令: /loop start | /loop status | /loop stop"

    def _show_current_goal(self) -> str:
        """显示当前 goal。"""
        if not self.current_goal:
            return "当前无活跃 Goal。使用 /goal new <目标> 创建。"

        goal = self.current_goal
        lines = [
            f"当前 Goal: {goal.goal_id}",
            f"目标: {goal.objective}",
            f"成功标准({len(goal.success_criteria)} 条):",
        ]
        lines.extend(f"  - {c}" for c in goal.success_criteria)
        lines.append(f"验证器: {len(goal.verification_plan)} 个")
        for v in goal.verification_plan:
            lines.append(f"  - {v.type}: {v.description or v.command or ''}")
        lines.append(
            f"预算: {goal.budgets.max_iterations}轮 / "
            f"{goal.budgets.max_runtime_minutes}分钟 / "
            f"{goal.budgets.max_tool_calls}次工具"
        )
        lines.append(f"工作区模式: {goal.workspace_mode}")
        return "\n".join(lines)

    def _create_goal_simple(self, text: str) -> str:
        """简化版创建 GoalSpec:解析 <objective> [:: <verify_command>]。"""
        from app.agent.goals import GoalSpec, VerificationCheck, GoalBudgets

        if "::" in text:
            objective, verify_cmd = text.split("::", 1)
            objective = objective.strip()
            verify_cmd = verify_cmd.strip()
        else:
            objective = text.strip()
            verify_cmd = None

        if not objective:
            return (
                "用法: /goal new <目标描述> [:: <验证命令>]\n"
                "示例: /goal new 修复登录按钮 :: pytest tests/test_login.py"
            )

        goal_id = f"goal-{uuid.uuid4().hex[:8]}"
        checks = []
        if verify_cmd:
            checks.append(VerificationCheck(
                type="command",
                command=verify_cmd,
                description=f"验证: {verify_cmd[:60]}",
            ))

        goal = GoalSpec(
            goal_id=goal_id,
            objective=objective,
            success_criteria=[objective],  # 简化:目标本身就是成功标准
            verification_plan=checks,
            budgets=GoalBudgets(max_iterations=6, max_runtime_minutes=30, max_tool_calls=80),
            workspace_mode="git_worktree",
        )

        errors = goal.validate()
        if errors:
            return f"GoalSpec 校验失败:\n" + "\n".join(f"  - {e}" for e in errors)

        # 保存到 storage
        from app.storage.loop_store import save_goal_spec
        save_goal_spec(self.storage.conn, _goal_to_dict(goal))

        self.current_goal = goal
        return f"已创建 Goal: {goal_id}\n{objective}\n验证器: {len(checks)} 个"

    def _show_goal(self, goal_id: str) -> str:
        """显示指定 goal。"""
        from app.storage.loop_store import load_goal_spec
        goal_dict = load_goal_spec(self.storage.conn, goal_id)
        if not goal_dict:
            return f"Goal 不存在: {goal_id}"

        return (
            f"Goal: {goal_dict['goal_id']}\n"
            f"目标: {goal_dict['objective']}\n"
            f"成功标准: {len(goal_dict['success_criteria'])} 条\n"
            f"验证器: {len(goal_dict['verification_plan'])} 个"
        )

    def _start_loop(self, goal_id: str | None) -> str:
        """启动 loop。"""
        # 解析 goal
        if goal_id:
            from app.storage.loop_store import load_goal_spec
            goal_dict = load_goal_spec(self.storage.conn, goal_id)
            if not goal_dict:
                return f"Goal 不存在: {goal_id}"
            goal = _dict_to_goal(goal_dict)
        elif self.current_goal:
            goal = self.current_goal
        else:
            return "请指定 goal_id 或先创建 goal (使用 /goal new)"

        # 校验
        errors = goal.validate()
        if errors:
            return "GoalSpec 校验失败:\n" + "\n".join(f"  - {e}" for e in errors)

        # 获取当前 session 的 Orchestrator
        session = self.get_session()
        if session is None:
            return "当前无活跃 session。"
        if not hasattr(session, "_orch") or session._orch is None:
            session._ensure_orchestrator()
        orchestrator = session._orch

        # Verifier:workspace_root 在 LoopRunner 准备 worktree 后会被覆盖
        from app.agent.verifier import Verifier
        verifier = Verifier(workspace_root=str(self.workspace_root))

        # WorktreeManager:只在 git_worktree 模式下创建
        worktree_manager = None
        if goal.workspace_mode == "git_worktree":
            try:
                from app.workspace.worktree import WorktreeManager
                worktree_manager = WorktreeManager(repo_root=self.workspace_root)
            except ValueError as e:
                return (
                    f"无法使用 git_worktree 模式: {e}\n"
                    "可修改 goal.workspace_mode 为 'direct' 或在 Git 仓库内运行。"
                )

        # 构建 LoopRunner
        from app.agent.loop_runner import LoopRunner
        loop = LoopRunner(
            goal=goal,
            orchestrator=orchestrator,
            verifier=verifier,
            worktree_manager=worktree_manager,
            on_event=session._on_run_event if hasattr(session, "_on_run_event") else None,
        )
        self.current_loop = loop

        # 执行(通过 CLI 提供的 run_loop_fn,带取消处理)
        result = self.run_loop_fn(loop)
        return result

    def _show_loop_status(self) -> str:
        """显示 loop 状态。"""
        if not self.current_loop:
            return "当前无运行中的 Loop。"

        loop = self.current_loop
        lines = [
            f"Loop: {loop.loop_id}",
            f"Goal: {loop.goal.objective[:80]}",
            f"状态: {loop.status}",
            f"迭代: {loop.current_iteration} / {loop.goal.budgets.max_iterations}",
            f"预算消耗: {loop.budget_used.tool_calls} 次工具调用",
        ]
        if loop.worktree:
            lines.append(f"Worktree: {loop.worktree.path}")
        return "\n".join(lines)
