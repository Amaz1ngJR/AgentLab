"""GoalSpec 单元测试。"""
import pytest

from app.agent.goals import GoalBudgets, GoalSpec, VerificationCheck


def test_goalspec_valid_minimal():
    """最小有效 GoalSpec。"""
    goal = GoalSpec(
        goal_id="test-1",
        objective="修复登录页按钮错位",
        success_criteria=["单元测试通过"],
        verification_plan=[
            VerificationCheck(type="command", command="pytest tests/"),
        ],
    )
    assert goal.is_valid()
    assert goal.validate() == []


def test_goalspec_empty_objective():
    """objective 为空应报错。"""
    goal = GoalSpec(
        goal_id="test-2",
        objective="",
        success_criteria=["测试通过"],
        verification_plan=[VerificationCheck(type="command", command="pytest")],
    )
    errors = goal.validate()
    assert any("objective" in e for e in errors)


def test_goalspec_empty_success_criteria():
    """success_criteria 为空应报错。"""
    goal = GoalSpec(
        goal_id="test-3",
        objective="修复 bug",
        success_criteria=[],
        verification_plan=[VerificationCheck(type="command", command="pytest")],
    )
    errors = goal.validate()
    assert any("success_criteria" in e for e in errors)


def test_goalspec_vague_success_criteria():
    """只有模糊标准应报错。"""
    goal = GoalSpec(
        goal_id="test-4",
        objective="优化性能",
        success_criteria=["效果变好"],
        verification_plan=[VerificationCheck(type="command", command="pytest")],
    )
    errors = goal.validate()
    assert any("模糊" in e for e in errors)


def test_goalspec_no_verifier():
    """没有 verifier 应报错。"""
    goal = GoalSpec(
        goal_id="test-5",
        objective="修复 bug",
        success_criteria=["测试通过"],
        verification_plan=[],
    )
    errors = goal.validate()
    assert any("verification_plan" in e for e in errors)


def test_goalspec_high_risk_only_llm_judge():
    """高风险目标只用 llm_judge 应报错。"""
    goal = GoalSpec(
        goal_id="test-6",
        objective="删除生产环境旧数据",
        success_criteria=["数据已清理"],
        verification_plan=[
            VerificationCheck(type="llm_judge", description="判断是否完成"),
        ],
    )
    errors = goal.validate()
    assert any("高风险" in e and "llm_judge" in e for e in errors)


def test_goalspec_invalid_budgets():
    """预算值 <= 0 应报错。"""
    goal = GoalSpec(
        goal_id="test-7",
        objective="修复 bug",
        success_criteria=["测试通过"],
        verification_plan=[VerificationCheck(type="command", command="pytest")],
        budgets=GoalBudgets(
            max_iterations=0,
            max_runtime_minutes=-1,
            max_tool_calls=100,
        ),
    )
    errors = goal.validate()
    assert len(errors) >= 2  # iterations 和 runtime


def test_goalspec_workspace_modes():
    """workspace_mode 三种模式都应合法。"""
    for mode in ["direct", "git_worktree", "remote_workspace"]:
        goal = GoalSpec(
            goal_id=f"test-mode-{mode}",
            objective="测试",
            success_criteria=["完成"],
            verification_plan=[VerificationCheck(type="human")],
            workspace_mode=mode,
        )
        assert goal.is_valid()


def test_goalspec_custom_budgets():
    """自定义预算应保留。"""
    budgets = GoalBudgets(
        max_iterations=10,
        max_runtime_minutes=60,
        max_tool_calls=200,
        max_cost_usd=5.0,
    )
    goal = GoalSpec(
        goal_id="test-8",
        objective="大型重构",
        success_criteria=["所有测试通过"],
        verification_plan=[VerificationCheck(type="command", command="pytest")],
        budgets=budgets,
    )
    assert goal.is_valid()
    assert goal.budgets.max_iterations == 10
    assert goal.budgets.max_cost_usd == 5.0


def test_goalspec_learning_policy():
    """learning_policy 默认值应正确。"""
    goal = GoalSpec(
        goal_id="test-9",
        objective="测试",
        success_criteria=["完成"],
        verification_plan=[VerificationCheck(type="human")],
    )
    assert goal.learning_policy["write_memory_candidates"] is True
    assert goal.learning_policy["propose_skill_updates"] is True
