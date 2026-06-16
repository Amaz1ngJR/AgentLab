"""Loop Engineering CLI 命令处理。

实现 /goal 和 /loop 命令族。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agent.goals import GoalSpec
    from app.agent.loop_runner import LoopRunner
    from app.storage import Storage


class LoopCommandHandler:
    """处理 /goal 和 /loop 命令。"""

    def __init__(self, storage: Storage):
        self.storage = storage
        self.current_goal: GoalSpec | None = None
        self.current_loop: LoopRunner | None = None

    def handle_goal_command(self, line: str) -> str | None:
        """处理 /goal 命令。

        支持：
          /goal                    # 显示当前 GoalSpec
          /goal new                # 交互式创建 GoalSpec（简化版）
          /goal show <goal_id>     # 显示指定 goal
        """
        parts = line.strip().split(maxsplit=2)
        if len(parts) == 1:
            # /goal - 显示当前
            return self._show_current_goal()

        subcommand = parts[1]

        if subcommand == "new":
            return self._create_goal_interactive()
        elif subcommand == "show":
            goal_id = parts[2] if len(parts) > 2 else None
            if not goal_id:
                return "用法: /goal show <goal_id>"
            return self._show_goal(goal_id)
        else:
            return (
                "未知子命令。支持:\n"
                "  /goal           - 显示当前 goal\n"
                "  /goal new       - 创建新 goal\n"
                "  /goal show <id> - 显示指定 goal"
            )

    def handle_loop_command(self, line: str) -> str | None:
        """处理 /loop 命令。

        支持：
          /loop start [goal_id]    # 启动 loop
          /loop status             # 显示状态
          /loop stop               # 停止（暂未实现）
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
            return "Loop 停止功能待实现（当前可用 Ctrl-C）"
        else:
            return "未知子命令: /loop start | /loop status | /loop stop"

    def _show_current_goal(self) -> str:
        """显示当前 goal。"""
        if not self.current_goal:
            return "当前无活跃 Goal。使用 /goal new 创建。"

        from app.agent.goals import GoalSpec
        goal = self.current_goal

        return (
            f"当前 Goal: {goal.goal_id}\n"
            f"目标: {goal.objective}\n"
            f"成功标准:\n" + "\n".join(f"  - {c}" for c in goal.success_criteria) +
            f"\n验证器: {len(goal.verification_plan)} 个\n"
            f"预算: {goal.budgets.max_iterations} 轮 / "
            f"{goal.budgets.max_runtime_minutes} 分钟 / "
            f"{goal.budgets.max_tool_calls} 次工具调用\n"
            f"工作区模式: {goal.workspace_mode}"
        )

    def _create_goal_interactive(self) -> str:
        """交互式创建 GoalSpec（简化版）。"""
        # 简化实现：返回创建说明
        return (
            "创建 GoalSpec（简化版 - 需要在代码中构造）\n\n"
            "示例代码:\n"
            "```python\n"
            "from app.agent.goals import GoalSpec, VerificationCheck, GoalBudgets\n"
            "goal = GoalSpec(\n"
            "    goal_id='test-goal',\n"
            "    objective='修复登录页按钮错位',\n"
            "    success_criteria=['单元测试通过', '页面正常显示'],\n"
            "    verification_plan=[\n"
            "        VerificationCheck(type='command', command='pytest tests/'),\n"
            "    ],\n"
            ")\n"
            "```\n\n"
            "完整交互式创建功能待实现。"
        )

    def _show_goal(self, goal_id: str) -> str:
        """显示指定 goal。"""
        # 从存储加载
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
        if not goal_id and not self.current_goal:
            return "请指定 goal_id 或先创建 goal (使用 /goal new)"

        # 简化实现
        return (
            "Loop 启动功能待完整实现。\n"
            "需要:\n"
            "  1. 加载 GoalSpec\n"
            "  2. 创建 LoopRunner\n"
            "  3. 绑定 Orchestrator/Verifier/WorktreeManager\n"
            "  4. 调用 loop.run()\n\n"
            "当前核心组件已实现，正在集成到 CLI。"
        )

    def _show_loop_status(self) -> str:
        """显示 loop 状态。"""
        if not self.current_loop:
            return "当前无运行中的 Loop。"

        loop = self.current_loop
        return (
            f"Loop: {loop.loop_id}\n"
            f"状态: {loop.status}\n"
            f"迭代: {loop.current_iteration} / {loop.goal.budgets.max_iterations}\n"
            f"预算消耗: {loop.budget_used.tool_calls} 次工具调用"
        )
