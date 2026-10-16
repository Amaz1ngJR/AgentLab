"""SubagentCoordinator 并行、隔离、失败传播和冲突治理测试。"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.agent.subagents import SubagentCoordinator, SubagentSpec
from app.workspace.worktree import WorktreeManager


@pytest.fixture
def git_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def _commit(workspace: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=workspace, check=True, capture_output=True)


def test_independent_writers_run_in_parallel_isolated_worktrees(git_repo):
    manager = WorktreeManager(git_repo)
    barrier = threading.Barrier(2)
    workspaces = {}

    def delegate(spec, workspace, cancel):
        workspaces[spec.subagent_id] = workspace
        barrier.wait(timeout=2)
        (workspace / f"{spec.subagent_id}.txt").write_text(spec.instruction, encoding="utf-8")
        _commit(workspace, spec.subagent_id)
        return f"done:{spec.subagent_id}"

    coordinator = SubagentCoordinator(manager, delegate, max_workers=2)
    results = coordinator.run([
        SubagentSpec("a", "write a"),
        SubagentSpec("b", "write b"),
    ])

    assert [results[key].status for key in results] == ["succeeded", "succeeded"]
    assert workspaces["a"] != workspaces["b"]
    assert results["a"].changed_files == ["a.txt"]
    assert results["b"].changed_files == ["b.txt"]
    assert not (git_repo / "a.txt").exists()
    assert all(item["mergeable"] for item in coordinator.merge_plan(results))


def test_same_file_changes_are_marked_conflict(git_repo):
    manager = WorktreeManager(git_repo)

    def delegate(spec, workspace, cancel):
        (workspace / "README.md").write_text(spec.subagent_id + "\n", encoding="utf-8")
        _commit(workspace, spec.subagent_id)
        return "done"

    coordinator = SubagentCoordinator(manager, delegate, max_workers=2)
    results = coordinator.run([
        SubagentSpec("a", "change readme"),
        SubagentSpec("b", "change readme"),
    ])

    assert results["a"].status == "conflict"
    assert results["b"].status == "conflict"
    assert results["a"].conflict_files == ["README.md"]
    assert not any(item["mergeable"] for item in coordinator.merge_plan(results))


def test_failed_dependency_blocks_dependent_agent(git_repo):
    manager = WorktreeManager(git_repo)
    called = []

    def delegate(spec, workspace, cancel):
        called.append(spec.subagent_id)
        if spec.subagent_id == "a":
            raise RuntimeError("boom")
        return "must not run"

    coordinator = SubagentCoordinator(manager, delegate, max_workers=2)
    results = coordinator.run([
        SubagentSpec("a", "fail", read_only=True),
        SubagentSpec("b", "depends", dependencies=("a",), read_only=True),
    ])

    assert results["a"].status == "failed"
    assert results["b"].status == "blocked"
    assert called == ["a"]


def test_read_only_agents_share_repo_without_worktrees(git_repo):
    manager = WorktreeManager(git_repo)
    seen = []

    def delegate(spec, workspace, cancel):
        seen.append(workspace)
        return (workspace / "README.md").read_text(encoding="utf-8")

    results = SubagentCoordinator(manager, delegate, max_workers=2).run([
        SubagentSpec("a", "read", read_only=True),
        SubagentSpec("b", "read", read_only=True),
    ])

    assert seen == [git_repo, git_repo]
    assert all(result.worktree is None for result in results.values())
    assert all(result.status == "succeeded" for result in results.values())


def test_merge_plan_rejects_main_branch_drift(git_repo):
    manager = WorktreeManager(git_repo)

    def delegate(spec, workspace, cancel):
        (workspace / "child.txt").write_text("child", encoding="utf-8")
        _commit(workspace, "child")
        return "done"

    coordinator = SubagentCoordinator(manager, delegate)
    results = coordinator.run([SubagentSpec("a", "write child")])
    (git_repo / "main.txt").write_text("main", encoding="utf-8")
    _commit(git_repo, "main advanced")

    plan = coordinator.merge_plan(results)
    assert plan[0]["mergeable"] is False
    assert "主分支" in plan[0]["reason"]


def test_invalid_dependency_is_rejected_before_worktree_creation(git_repo):
    manager = WorktreeManager(git_repo)
    coordinator = SubagentCoordinator(manager, lambda *args: "done")

    with pytest.raises(ValueError, match="依赖不存在"):
        coordinator.run([SubagentSpec("a", "work", dependencies=("missing",))])

    assert not (git_repo / "data" / "worktrees").exists()


def test_real_result_collects_changes_and_emits_only_final_conflicts(git_repo):
    from app.agent.subagents import SubagentResult
    events = []

    def delegate(spec, workspace, cancel):
        (workspace / "README.md").write_text(spec.subagent_id)
        return SubagentResult(spec.subagent_id, spec.role, "succeeded", output="edited")

    results = SubagentCoordinator(WorktreeManager(git_repo), delegate, on_event=events.append).run([
        SubagentSpec("a", "edit"), SubagentSpec("b", "edit"),
    ])
    assert all(r.changed_files == ["README.md"] for r in results.values())
    completed = [e for e in events if e.kind == "subagent_completed"]
    assert len(completed) == 2
    assert all(e.payload["status"] == "conflict" for e in completed)


def test_merge_plan_rejects_committed_but_still_dirty_worktree(git_repo):
    def delegate(spec, workspace, cancel):
        (workspace / "child.txt").write_text("committed")
        _commit(workspace, "child")
        (workspace / "child.txt").write_text("not committed")
        return "done"

    coordinator = SubagentCoordinator(WorktreeManager(git_repo), delegate)
    results = coordinator.run([SubagentSpec("a", "edit")])
    plan = coordinator.merge_plan(results)
    assert not plan[0]["mergeable"]
    assert "未提交" in plan[0]["reason"]


def test_merge_plan_rejects_uncommitted_parent_drift(git_repo):
    def delegate(spec, workspace, cancel):
        (workspace / "child.txt").write_text("committed")
        _commit(workspace, "child")
        return "done"

    coordinator = SubagentCoordinator(WorktreeManager(git_repo), delegate)
    results = coordinator.run([SubagentSpec("a", "edit")])
    (git_repo / "README.md").write_text("parent drift without commit")
    assert not coordinator.merge_plan(results)[0]["mergeable"]


def test_coordinator_uses_separate_child_mailboxes(git_repo):
    from app.agent.cancel import CancelToken
    parent = CancelToken()
    parent.steer("parent only")
    tokens = []

    def delegate(spec, workspace, cancel):
        tokens.append(cancel)
        assert not cancel.has_steering
        return "done"

    SubagentCoordinator(WorktreeManager(git_repo), delegate).run([
        SubagentSpec("a", "read", read_only=True), SubagentSpec("b", "read", read_only=True),
    ], cancel=parent)
    assert tokens[0] is not tokens[1] and parent not in tokens
    assert parent.drain_steering() == ["parent only"]


def test_mergeable_snapshot_is_refreshed_after_workspace_changes(git_repo):
    def delegate(spec, workspace, cancel):
        (workspace / "child.txt").write_text("ready")
        _commit(workspace, "child")
        return "done"

    coordinator = SubagentCoordinator(WorktreeManager(git_repo), delegate)
    results = coordinator.run([SubagentSpec("a", "edit")])
    assert not results["a"].mergeable  # 尚未校验
    assert coordinator.merge_plan(results)[0]["mergeable"]
    assert results["a"].mergeable
    (results["a"].workspace / "child.txt").write_text("dirty again")
    assert not coordinator.merge_plan(results)[0]["mergeable"]
    assert not results["a"].mergeable


def test_parent_task_dependency_allows_real_writer_but_not_delivery(git_repo):
    import json
    from app.agent.approval import AutoApprove
    from app.agent.orchestrator import Orchestrator
    from app.config.loader import use_workspace_root
    from app.models.protocol import ModelResponse, ToolCall
    from app.tools.builtin.files import default_tools
    from app.tools.registry import ToolRegistry
    from tests.unit.test_multiagent_handoff import SnapshotRouter
    from tests.unit.test_execution_plan import response

    tools = ToolRegistry()
    for tool in default_tools():
        tools.register(tool)
    plan = {"version": 1, "tasks": [
        {"id": "before", "content": "准备"},
        {"id": "write", "content": "创建文件", "dependencies": ["before"], "executor": "subagent",
         "subagent": {"id": "writer", "instruction": "创建文件", "role": "executor",
                      "read_only": False, "tools": ["write_file"]}},
    ]}
    router = SnapshotRouter([
        response(json.dumps(plan)), response("准备完成"),
        response(json.dumps({"tasks": [{"id": "c", "content": "创建文件"}]})),
        ModelResponse("", [ToolCall("w", "write_file", {"path": "child.txt", "content": "new"})],
                      {"input_tokens": 1, "output_tokens": 1}, []), response("已创建文件"),
    ])
    events = []
    with use_workspace_root(git_repo):
        orch = Orchestrator(router, tools, AutoApprove(), on_event=events.append)
        orch.run("准备后创建文件")
    result = orch.subagent_results["writer"]
    assert result.status == "succeeded"
    assert result.changed_files == ["child.txt"]
    assert not (git_repo / "child.txt").exists()
    assert orch.last_run_status == "blocked"
    saved = orch.store.get("write").execution["result"]
    assert saved["delivery_status"] == "pending_review"
    assert saved["worktree"]["base_commit"] == result.worktree.base_commit
    assert [event.kind for event in events].count("subagent_completed") == 1


def test_cancel_preserves_writer_artifact_and_terminal_event(git_repo):
    from app.agent.cancel import CancelToken
    token = CancelToken()
    events = []

    def delegate(spec, workspace, cancel):
        (workspace / "partial.txt").write_text("partial")
        token.cancel()
        cancel.raise_if_cancelled()

    results = SubagentCoordinator(WorktreeManager(git_repo), delegate, on_event=events.append).run([
        SubagentSpec("a", "write"), SubagentSpec("b", "wait", dependencies=("a",)),
    ], cancel=token)
    assert results["a"].status == results["b"].status == "cancelled"
    assert results["a"].changed_files == ["partial.txt"]
    assert results["a"].worktree.path.exists()
    assert len([e for e in events if e.kind == "subagent_completed"]) == 2
