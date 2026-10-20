"""Loop 证据存储和 CLI evidence/diff/resume 的离线测试。"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.agent.approval import AutoApprove
from app.agent.goals import GoalBudgets, GoalSpec, VerificationCheck
from app.agent.loop_commands import LoopCommandHandler, _goal_to_dict
from app.agent.loop_runner import LoopRunner
from app.agent.verifier import CheckResult, VerificationResult
from app.storage import Storage
from app.storage.loop_store import (
    load_loop_diff,
    load_loop_evidence,
    save_goal_spec,
    save_loop_artifact,
    save_loop_run,
)


def _goal(session_id="s1"):
    return GoalSpec(
        goal_id="goal-evidence",
        objective="生成结果",
        success_criteria=["验证通过"],
        verification_plan=[VerificationCheck(type="file_assertion", path="x", exists=True)],
        budgets=GoalBudgets(max_iterations=2, max_runtime_minutes=5, max_tool_calls=10),
        workspace_mode="direct",
        session_id=session_id,
    )


def test_loop_runner_persists_run_iteration_and_verification(tmp_path):
    storage = Storage(tmp_path / "db")
    storage.create_session("s1", "default", "fake", "test")
    goal = _goal()
    save_goal_spec(storage.conn, _goal_to_dict(goal))

    orchestrator = MagicMock()
    orchestrator.last_run_status = "completed"
    orchestrator.last_run_tool_calls = 2
    orchestrator.task_store.snapshot.return_value = [{"id": "t1", "status": "completed"}]
    verifier = MagicMock()
    verifier.verify.return_value = VerificationResult(
        status="pass",
        checks=[CheckResult(name="file", status="pass", summary="ok")],
    )
    loop = LoopRunner(
        goal, orchestrator, verifier, storage=storage, session_id="s1",
    )

    loop.run()
    evidence = load_loop_evidence(storage.conn, loop.loop_id)
    assert evidence["status"] == "succeeded"
    assert evidence["budget_used"]["tool_calls"] == 2
    assert evidence["iterations"][0]["status"] == "succeeded"
    assert evidence["verifications"][0]["checks"][0]["name"] == "file"
    assert evidence["finished_at"] is not None


def test_loop_artifact_is_redacted_bounded_and_diff_loadable(tmp_path):
    storage = Storage(tmp_path / "db")
    storage.create_session("s1", "default", "fake", "test")
    goal = _goal()
    save_goal_spec(storage.conn, _goal_to_dict(goal))
    now = datetime.utcnow().isoformat()
    save_loop_run(storage.conn, {
        "id": "loop-1", "goal_id": goal.goal_id, "session_id": "s1",
        "status": "succeeded", "started_at": now,
    })
    artifact_id = save_loop_artifact(storage.conn, {
        "loop_id": "loop-1", "kind": "diff",
        "content": "Authorization: Bearer secret-token\n" + "x" * 60000,
        "metadata": {"commit_sha": "abc"},
    })
    artifact = load_loop_diff(storage.conn, "loop-1")
    assert artifact["id"] == artifact_id
    assert "secret-token" not in artifact["content"]
    assert len(artifact["content"]) <= 50000
    assert artifact["metadata"]["commit_sha"] == "abc"


def test_loop_commands_show_evidence_diff_and_resume(tmp_path):
    storage = Storage(tmp_path / "db")
    storage.create_session("s1", "default", "fake", "test")
    goal = _goal()
    save_goal_spec(storage.conn, _goal_to_dict(goal))
    now = datetime.utcnow().isoformat()
    save_loop_run(storage.conn, {
        "id": "loop-resume", "goal_id": goal.goal_id, "session_id": "s1",
        "status": "cancelled", "current_iteration": 1,
        "budget_used": {"iterations": 1, "tool_calls": 3}, "started_at": now,
    })
    save_loop_artifact(storage.conn, {
        "loop_id": "loop-resume", "kind": "diff", "content": "M app/x.py",
    })
    session = SimpleNamespace(session_id="s1")
    handler = LoopCommandHandler(storage, lambda: session, lambda loop: "resumed", tmp_path)

    assert "loop-resume" in handler.handle_loop_command("/loop evidence loop-resume")
    assert "M app/x.py" in handler.handle_loop_command("/loop diff loop-resume")
    handler._start_loop = MagicMock(return_value="resumed")
    assert handler.handle_loop_command("/loop resume loop-resume") == "resumed"
    resume_run = handler._start_loop.call_args.kwargs["resume_run"]
    assert resume_run["budget_used"]["tool_calls"] == 3
