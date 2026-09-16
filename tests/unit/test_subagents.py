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
