"""GoalSpec —— Loop Engineering 的目标定义。

用户通过 `/goal new` 定义目标、验收标准、约束和预算，LoopRunner 在这些边界内
持续执行、验证、修复，直到目标达成或被阻塞。

GoalSpec 与 Task 的区别：
  - Task 是"把事情做完"（编排已完成，见 planner/executor/replanner）
  - Goal 是"把事情做对"（执行 + 验证证据 + 沉淀经验，本模块待实现）

PRD §7.6.2
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class VerificationCheck:
    """单个验证检查项的定义。"""
    type: Literal["command", "file_assertion", "browser", "api",
                  "database_readonly", "remote", "human", "llm_judge"]
    # command 类型
    command: str | None = None
    expected_exit_code: int = 0
    # file_assertion 类型
    path: str | None = None
    exists: bool | None = None
    contains: str | None = None
    not_contains: str | None = None
    # browser 类型
    url: str | None = None
    assertion: str | None = None
    # api 类型
    method: str | None = None
    endpoint: str | None = None
    expected_status: int | None = None
    # 通用
    timeout: int = 30
    retry_on_flaky: int = 0
    description: str | None = None


@dataclass
class GoalBudgets:
    """Loop 执行预算上限。"""
    max_iterations: int = 6
    max_runtime_minutes: int = 30
    max_tool_calls: int = 80
    max_cost_usd: float | None = None


@dataclass
class GoalSpec:
    """Loop Engineering 的目标定义。

    必填字段：
      - objective: 自然语言目标描述
      - success_criteria: 至少一条可验证的成功标准
      - verification_plan: 至少一个 verifier（无 verifier 时必须要求 human 确认）

    可选字段：
      - constraints: 路径、工具、模型、网络、远程目标限制
      - budgets: iteration/时间/工具/成本上限
      - stop_conditions: 停止条件列表
      - workspace_mode: direct/git_worktree/remote_workspace
      - learning_policy: 是否生成记忆候选、Skill 改进建议
    """
    goal_id: str
    objective: str
    success_criteria: list[str]
    verification_plan: list[VerificationCheck]

    # 可选字段
    constraints: dict[str, list[str]] = field(default_factory=dict)
    budgets: GoalBudgets = field(default_factory=GoalBudgets)
    stop_conditions: list[str] = field(default_factory=lambda: [
        "verification_passed",
        "approval_denied",
        "budget_exhausted",
        "destructive_action_requested",
    ])
    workspace_mode: Literal["direct", "git_worktree", "remote_workspace"] = "git_worktree"
    learning_policy: dict[str, bool] = field(default_factory=lambda: {
        "write_memory_candidates": True,
        "propose_skill_updates": True,
    })

    # 元数据
    session_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def validate(self) -> list[str]:
        """校验 GoalSpec，返回错误列表（空列表表示通过）。

        校验规则（PRD §7.6.2）：
          1. objective 不能为空
          2. success_criteria 至少一条可验证标准（不能只写"效果变好"）
          3. verification_plan 至少一个 verifier；无 verifier 时必须有 human 类型
          4. 高风险目标不能只用 llm_judge 作为唯一验证器
          5. 预算值必须 > 0
        """
        errors = []

        # 1. objective
        if not self.objective or not self.objective.strip():
            errors.append("objective 不能为空")

        # 2. success_criteria
        if not self.success_criteria:
            errors.append("success_criteria 至少需要一条")
        else:
            vague_keywords = ["效果", "变好", "更好", "优化", "改进"]
            all_vague = all(
                any(kw in criterion for kw in vague_keywords)
                for criterion in self.success_criteria
            )
            if all_vague and len(self.success_criteria) < 2:
                errors.append(
                    "success_criteria 不能只写模糊标准（如'效果变好'），"
                    "需要可验证的具体标准（如'测试通过'、'文件存在'）"
                )

        # 3. verification_plan
        if not self.verification_plan:
            errors.append(
                "verification_plan 至少需要一个 verifier。"
                "若无法自动验证，请添加 type='human' 的人工确认检查。"
            )
        else:
            # 高风险目标不能只用 llm_judge
            only_llm_judge = all(
                check.type == "llm_judge" for check in self.verification_plan
            )
            if only_llm_judge and len(self.verification_plan) >= 1:
                # 简单启发式：包含"删除"、"部署"、"发布"视为高风险
                high_risk_keywords = ["删除", "部署", "发布", "上线", "生产"]
                is_high_risk = any(
                    kw in self.objective for kw in high_risk_keywords
                )
                if is_high_risk:
                    errors.append(
                        "高风险目标不能只用 llm_judge 验证，"
                        "需要至少一个 command/file_assertion/browser/api 等客观验证器。"
                    )

        # 4. 预算值
        if self.budgets.max_iterations <= 0:
            errors.append("budgets.max_iterations 必须 > 0")
        if self.budgets.max_runtime_minutes <= 0:
            errors.append("budgets.max_runtime_minutes 必须 > 0")
        if self.budgets.max_tool_calls <= 0:
            errors.append("budgets.max_tool_calls 必须 > 0")
        if self.budgets.max_cost_usd is not None and self.budgets.max_cost_usd <= 0:
            errors.append("budgets.max_cost_usd 必须 > 0 或 None")

        return errors

    def is_valid(self) -> bool:
        """快速判断 GoalSpec 是否有效。"""
        return len(self.validate()) == 0
