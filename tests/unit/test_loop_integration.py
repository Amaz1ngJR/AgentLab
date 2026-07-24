"""Loop Engineering 集成测试 —— 验证 LoopRunner 与 Orchestrator 的真实集成。

覆盖：
  - LoopRunner 真实调用 Orchestrator.run()
  - 工具调用计数累积
  - 验证失败后诊断修复(追加修复任务)
  - Ctrl-C 取消
  - 预算耗尽
  - worktree cwd 隔离(用 fake 模拟)
"""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.agent.cancel import CancelToken
from app.agent.approval import AutoApprove, DenyAll
from app.agent.goals import GoalSpec, VerificationCheck, GoalBudgets
from app.agent.loop_runner import LoopRunner
from app.agent.orchestrator import Orchestrator
from app.agent.planner import Planner
from app.agent.tasks import TaskStore
from app.agent.verifier import CheckResult, VerificationResult, Verifier
from app.models.protocol import ModelResponse, ToolCall
from app.tools.registry import ToolRegistry
from app.workspace.worktree import WorktreeInfo


def _fake_llm():
    """返回一个假 LLM,模拟规划与执行的模型调用。"""
    llm = MagicMock()
    # 规划阶段:返回 JSON 计划
    plan_json = """
    [
        {"id": "t1", "description": "写一个测试文件", "dependencies": []}
    ]
    """
    responses = [
        # 第一次调用:规划阶段,返回计划 JSON
        ModelResponse(
            text=plan_json,
            tool_calls=[],
            finish_reason="end_turn",
            provider_payload=[{"role": "assistant", "content": plan_json}],
            usage={"input_tokens": 100, "output_tokens": 50},
        ),
        # 第二次调用:执行阶段,调用 write_file 工具
        ModelResponse(
            text="",
            tool_calls=[
                ToolCall(
                    id="call1",
                    name="write_file",
                    arguments={"path": "test.txt", "content": "hello"},
                )
            ],
            finish_reason="tool_use",
            provider_payload=[
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call1",
                            "name": "write_file",
                            "input": {"path": "test.txt", "content": "hello"},
                        }
                    ],
                }
            ],
            usage={"input_tokens": 150, "output_tokens": 30},
        ),
        # 第三次调用:执行结束,返回完成文本
        ModelResponse(
            text="已完成文件创建。",
            tool_calls=[],
            finish_reason="end_turn",
            provider_payload=[{"role": "assistant", "content": "已完成文件创建。"}],
            usage={"input_tokens": 200, "output_tokens": 20},
        ),
    ]
    # Loop 的修复路径会多次调用同一个 Orchestrator；每轮都需要完整的
    # plan -> tool_use -> end_turn 三段响应。
    llm.create_message.side_effect = responses * 4
    return llm


def _fake_write_tool():
    """返回一个假的 write_file 工具。"""
    from app.tools.registry import Tool

    def _write(args):
        path = args.get("path", "")
        content = args.get("content", "")
        return f"写入 {len(content)} 字节到 {path}"

    return Tool(
        name="write_file",
        description="写文件",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        executor=_write,
        requires_approval=False,
    )


def test_loop_runner_executes_with_orchestrator():
    """LoopRunner 真实调用 Orchestrator,累计工具调用数。"""
    llm = _fake_llm()
    tools = ToolRegistry()
    tools.register(_fake_write_tool())
    task_store = TaskStore()
    planner = Planner(llm)

    orchestrator = Orchestrator(
        llm=llm,
        tools=tools,
        system="test",
        task_store=task_store,
        planner=planner,
        messages=[],
    )

    # 验证器:command 检查(模拟通过)
    verifier = Verifier(workspace_root="/tmp")

    # 模拟验证通过(不实际运行命令)
    def _fake_verify(checks):
        from app.agent.verifier import VerificationResult, CheckResult

        return VerificationResult(
            status="pass",
            checks=[
                CheckResult(
                    name="test",
                    status="pass",
                    summary="验证通过",
                )
            ],
        )

    verifier.verify = _fake_verify

    goal = GoalSpec(
        goal_id="test-goal",
        objective="创建测试文件",
        success_criteria=["文件存在"],
        verification_plan=[
            VerificationCheck(type="command", command="echo ok"),
        ],
        budgets=GoalBudgets(max_iterations=3, max_runtime_minutes=10, max_tool_calls=50),
        workspace_mode="direct",  # 不用 worktree,简化测试
    )

    loop = LoopRunner(
        goal=goal,
        orchestrator=orchestrator,
        verifier=verifier,
        worktree_manager=None,
    )

    result = loop.run()

    # 验证:loop 成功完成
    assert loop.status == "succeeded"
    assert loop.current_iteration == 1
    # 验证工具调用计数(write_file 被调用一次)
    assert loop.budget_used.tool_calls >= 1
    assert "目标达成" in result


def test_loop_runner_repair_on_verification_failure():
    """验证失败后,LoopRunner 生成修复指令并继续。"""
    llm = _fake_llm()
    tools = ToolRegistry()
    tools.register(_fake_write_tool())
    task_store = TaskStore()
    planner = Planner(llm)

    orchestrator = Orchestrator(
        llm=llm,
        tools=tools,
        system="test",
        task_store=task_store,
        planner=planner,
        messages=[],
    )

    verifier = Verifier(workspace_root="/tmp")

    # 第一次验证失败,第二次通过
    call_count = [0]

    def _fake_verify_flaky(checks):
        from app.agent.verifier import VerificationResult, CheckResult

        call_count[0] += 1
        if call_count[0] == 1:
            # 第一次:失败
            return VerificationResult(
                status="fail",
                checks=[
                    CheckResult(
                        name="test",
                        status="fail",
                        summary="文件内容错误",
                        error="missing expected text",
                    )
                ],
                failure_category="test_failed",
                next_hint="文件内容不符合预期",
            )
        else:
            # 第二次:通过
            return VerificationResult(
                status="pass",
                checks=[
                    CheckResult(
                        name="test",
                        status="pass",
                        summary="验证通过",
                    )
                ],
            )

    verifier.verify = _fake_verify_flaky

    goal = GoalSpec(
        goal_id="test-goal",
        objective="创建正确的测试文件",
        success_criteria=["文件内容正确"],
        verification_plan=[
            VerificationCheck(type="command", command="grep expected test.txt"),
        ],
        budgets=GoalBudgets(max_iterations=3, max_runtime_minutes=10, max_tool_calls=50),
        workspace_mode="direct",
    )

    loop = LoopRunner(
        goal=goal,
        orchestrator=orchestrator,
        verifier=verifier,
        worktree_manager=None,
    )

    result = loop.run()

    # 验证:loop 经过修复后成功
    assert loop.status == "succeeded"
    assert loop.current_iteration == 2  # 第一轮失败,第二轮修复成功
    assert "目标达成" in result


def test_loop_runner_budget_exhausted():
    """预算耗尽时 loop 停止。"""
    llm = _fake_llm()
    tools = ToolRegistry()
    tools.register(_fake_write_tool())
    task_store = TaskStore()
    planner = Planner(llm)

    orchestrator = Orchestrator(
        llm=llm,
        tools=tools,
        system="test",
        task_store=task_store,
        planner=planner,
        messages=[],
    )

    verifier = Verifier(workspace_root="/tmp")

    # 验证永远失败
    def _fake_verify_always_fail(checks):
        from app.agent.verifier import VerificationResult, CheckResult

        return VerificationResult(
            status="fail",
            checks=[
                CheckResult(
                    name="test",
                    status="fail",
                    summary="永远失败",
                )
            ],
            failure_category="test_failed",
        )

    verifier.verify = _fake_verify_always_fail

    goal = GoalSpec(
        goal_id="test-goal",
        objective="不可能完成的任务",
        success_criteria=["不可能达成"],
        verification_plan=[
            VerificationCheck(type="command", command="false"),
        ],
        budgets=GoalBudgets(max_iterations=2, max_runtime_minutes=10, max_tool_calls=50),
        workspace_mode="direct",
    )

    loop = LoopRunner(
        goal=goal,
        orchestrator=orchestrator,
        verifier=verifier,
        worktree_manager=None,
    )

    result = loop.run()

    # 验证:预算耗尽
    assert loop.status == "budget_exhausted"
    assert loop.current_iteration == 2  # 达到 max_iterations
    assert "预算耗尽" in result


def test_loop_runner_execution_exception_cannot_be_verified_as_success():
    """执行器崩溃时必须直接失败，不能继续用旧状态验证成成功。"""
    orchestrator = MagicMock()
    orchestrator.run.side_effect = RuntimeError("executor crashed")
    orchestrator.last_run_tool_calls = 0
    verifier = MagicMock()
    verifier.verify.return_value = MagicMock(is_success=lambda: True)
    goal = GoalSpec(
        goal_id="test-execution-error",
        objective="执行任务",
        success_criteria=["任务成功执行"],
        verification_plan=[
            VerificationCheck(type="file_assertion", path="existing.txt", exists=True),
        ],
        budgets=GoalBudgets(max_iterations=2, max_runtime_minutes=10, max_tool_calls=10),
        workspace_mode="direct",
    )
    loop = LoopRunner(
        goal=goal,
        orchestrator=orchestrator,
        verifier=verifier,
    )

    result = loop.run()

    assert loop.status == "failed"
    assert "executor crashed" in result
    verifier.verify.assert_not_called()


def test_loop_success_commits_worktree_after_approval(tmp_path):
    """验证通过后，只有审批放行才生成可合并提交。"""
    manager = MagicMock()
    manager.get_diff_summary.return_value = "工作区状态:\n?? result.txt"
    manager.check_dirty.return_value = True
    manager.commit_all.return_value = "a" * 40
    manager.merge_suggestion.return_value = "git merge worktree/test"
    goal = GoalSpec(
        goal_id="test-commit",
        objective="生成结果",
        success_criteria=["结果存在"],
        verification_plan=[
            VerificationCheck(type="file_assertion", path="result.txt", exists=True),
        ],
        workspace_mode="git_worktree",
    )
    loop = LoopRunner(
        goal=goal,
        orchestrator=MagicMock(),
        verifier=MagicMock(),
        worktree_manager=manager,
        approval=AutoApprove(),
    )
    loop.worktree = WorktreeInfo(
        worktree_id="test",
        path=tmp_path,
        base_branch="main",
        base_commit="0" * 40,
    )
    verification = VerificationResult(
        status="pass",
        checks=[CheckResult(name="file", status="pass")],
    )

    result = loop._finish_succeeded(verification)

    manager.commit_all.assert_called_once()
    assert "已生成验证提交" in result


def test_loop_success_keeps_dirty_worktree_when_commit_denied(tmp_path):
    """拒绝提交后保留改动，不能静默生成 commit。"""
    manager = MagicMock()
    manager.get_diff_summary.return_value = "工作区状态:\n?? result.txt"
    manager.check_dirty.return_value = True
    manager.merge_suggestion.return_value = "不能直接合并"
    goal = GoalSpec(
        goal_id="test-denied-commit",
        objective="生成结果",
        success_criteria=["结果存在"],
        verification_plan=[
            VerificationCheck(type="file_assertion", path="result.txt", exists=True),
        ],
        workspace_mode="git_worktree",
    )
    loop = LoopRunner(
        goal=goal,
        orchestrator=MagicMock(),
        verifier=MagicMock(),
        worktree_manager=manager,
        approval=DenyAll(),
    )
    loop.worktree = WorktreeInfo(
        worktree_id="test",
        path=tmp_path,
        base_branch="main",
        base_commit="0" * 40,
    )
    verification = VerificationResult(
        status="pass",
        checks=[CheckResult(name="file", status="pass")],
    )

    result = loop._finish_succeeded(verification)

    manager.commit_all.assert_not_called()
    assert "用户未批准生成提交" in result


def test_loop_runner_cancel():
    """Ctrl-C 取消时 loop 干净停止。"""
    llm = _fake_llm()
    tools = ToolRegistry()
    tools.register(_fake_write_tool())
    task_store = TaskStore()
    planner = Planner(llm)

    orchestrator = Orchestrator(
        llm=llm,
        tools=tools,
        system="test",
        task_store=task_store,
        planner=planner,
        messages=[],
    )

    verifier = Verifier(workspace_root="/tmp")

    goal = GoalSpec(
        goal_id="test-goal",
        objective="任务",
        success_criteria=["标准"],
        verification_plan=[
            VerificationCheck(type="command", command="echo ok"),
        ],
        budgets=GoalBudgets(max_iterations=10, max_runtime_minutes=10, max_tool_calls=50),
        workspace_mode="direct",
    )

    loop = LoopRunner(
        goal=goal,
        orchestrator=orchestrator,
        verifier=verifier,
        worktree_manager=None,
    )

    # 预先取消
    cancel = CancelToken()
    cancel.cancel()

    result = loop.run(cancel=cancel)

    # 验证:已取消
    assert loop.status == "cancelled"
    assert "已取消" in result
