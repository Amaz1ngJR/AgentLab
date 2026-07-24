"""WorktreeManager 单元测试。"""
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.workspace.worktree import WorktreeInfo, WorktreeManager


@pytest.fixture
def git_repo():
    """创建临时 Git 仓库。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        # 初始化 git 仓库
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=repo_path, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo_path, check=True, capture_output=True
        )
        # 创建初始提交
        (repo_path / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_path, check=True, capture_output=True
        )
        yield repo_path


def test_worktree_manager_init_valid_repo(git_repo):
    """在有效 Git 仓库中初始化。"""
    manager = WorktreeManager(repo_root=git_repo)
    assert manager.repo_root == git_repo


def test_worktree_manager_init_invalid_repo():
    """在非 Git 仓库中初始化应报错。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(ValueError, match="不是 Git 仓库"):
            WorktreeManager(repo_root=tmpdir)


def test_worktree_create(git_repo):
    """创建 worktree。"""
    manager = WorktreeManager(repo_root=git_repo)
    worktree = manager.create("test-goal-1")

    assert worktree.worktree_id == "test-goal-1"
    assert worktree.path.exists()
    assert worktree.path.name == "test-goal-1"
    assert worktree.base_branch == "master" or worktree.base_branch == "main"
    assert len(worktree.base_commit) == 40  # SHA-1


def test_worktree_create_duplicate_fails(git_repo):
    """重复创建应报错。"""
    manager = WorktreeManager(repo_root=git_repo)
    manager.create("test-goal-2")

    with pytest.raises(ValueError, match="Worktree 已存在"):
        manager.create("test-goal-2")


def test_worktree_create_require_clean_base(git_repo):
    """require_clean_base=True 时，有未提交改动应报错。"""
    # 添加未提交改动
    (git_repo / "dirty.txt").write_text("uncommitted")

    manager = WorktreeManager(repo_root=git_repo)
    with pytest.raises(ValueError, match="原工作区有未提交改动"):
        manager.create("test-goal-3", require_clean_base=True)


def test_worktree_get_diff_summary(git_repo):
    """获取 diff 摘要。"""
    manager = WorktreeManager(repo_root=git_repo)
    worktree = manager.create("test-goal-4")

    # 在 worktree 中做改动
    (worktree.path / "new_file.txt").write_text("hello")
    subprocess.run(
        ["git", "add", "new_file.txt"],
        cwd=worktree.path,
        check=True,
        capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "Add new file"],
        cwd=worktree.path,
        check=True,
        capture_output=True
    )

    diff = manager.get_diff_summary(worktree)
    assert "new_file.txt" in diff
    assert "改动统计" in diff or "文件列表" in diff


def test_worktree_diff_summary_includes_untracked_files(git_repo):
    """Agent 新建但尚未 add 的文件也必须出现在交付摘要中。"""
    manager = WorktreeManager(repo_root=git_repo)
    worktree = manager.create("test-goal-untracked")
    (worktree.path / "new_file.txt").write_text("hello")

    diff = manager.get_diff_summary(worktree)

    assert "new_file.txt" in diff
    assert "??" in diff
    assert "无改动" not in diff


def test_worktree_commit_all_makes_branch_mergeable(git_repo):
    """经审批调用 commit_all 后，分支应包含可合并提交。"""
    manager = WorktreeManager(repo_root=git_repo)
    worktree = manager.create("test-goal-commit")
    (worktree.path / "new_file.txt").write_text("hello")

    commit_sha = manager.commit_all(worktree, "verified changes")

    assert len(commit_sha) == 40
    assert not manager.check_dirty(worktree)
    assert manager.has_commits(worktree)
    assert "git merge" in manager.merge_suggestion(worktree)


def test_worktree_check_dirty(git_repo):
    """检查 worktree 是否 dirty。"""
    manager = WorktreeManager(repo_root=git_repo)
    worktree = manager.create("test-goal-5")

    # 初始状态：clean
    assert not manager.check_dirty(worktree)

    # 添加未提交改动
    (worktree.path / "dirty.txt").write_text("uncommitted")
    assert manager.check_dirty(worktree)


def test_worktree_remove_clean(git_repo):
    """删除 clean worktree。"""
    manager = WorktreeManager(repo_root=git_repo)
    worktree = manager.create("test-goal-6")

    manager.remove(worktree)
    assert not worktree.path.exists()


def test_worktree_remove_dirty_without_force_fails(git_repo):
    """删除 dirty worktree 需要 force。"""
    manager = WorktreeManager(repo_root=git_repo)
    worktree = manager.create("test-goal-7")

    # 添加未提交改动
    (worktree.path / "dirty.txt").write_text("uncommitted")

    with pytest.raises(ValueError, match="有未提交改动"):
        manager.remove(worktree)


def test_worktree_remove_dirty_with_force(git_repo):
    """force=True 可删除 dirty worktree。"""
    manager = WorktreeManager(repo_root=git_repo)
    worktree = manager.create("test-goal-8")

    # 添加未提交改动
    (worktree.path / "dirty.txt").write_text("uncommitted")

    manager.remove(worktree, force=True)
    assert not worktree.path.exists()


def test_worktree_merge_suggestion(git_repo):
    """生成合并建议。"""
    manager = WorktreeManager(repo_root=git_repo)
    worktree = manager.create("test-goal-9")
    (worktree.path / "result.txt").write_text("verified")
    manager.commit_all(worktree, "verified changes")

    suggestion = manager.merge_suggestion(worktree)
    assert "git checkout" in suggestion
    assert "git merge" in suggestion
    assert worktree.base_branch in suggestion
    assert f"worktree/{worktree.worktree_id}" in suggestion


def test_worktree_merge_suggestion_rejects_dirty_worktree(git_repo):
    """未提交改动不能被描述为可直接 merge。"""
    manager = WorktreeManager(repo_root=git_repo)
    worktree = manager.create("test-goal-dirty-merge")
    (worktree.path / "result.txt").write_text("verified")

    suggestion = manager.merge_suggestion(worktree)

    assert "不能直接合并" in suggestion
    assert "git merge worktree/" not in suggestion
