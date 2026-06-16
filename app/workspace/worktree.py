"""WorktreeManager —— Git worktree 隔离工作区。

Loop 模式的代码修改默认在隔离 worktree 内完成，验证通过后再由用户决定合并。

职责（PRD §7.6.6）：
  1. 从当前仓库创建隔离 worktree，命名包含 goal_id 或 loop_id
  2. 记录 base branch、base commit、worktree path 和 dirty state
  3. 所有写文件、shell、测试默认在 worktree 内执行
  4. loop 成功后生成 diff summary、测试证据和合并建议
  5. 合并、删除 worktree、覆盖主分支等动作必须由用户确认
  6. 如果原工作区已有用户未提交改动，不得自动覆盖或移动

安全边界：
  - 只在 Git 仓库内工作（非 Git 仓库抛出错误）
  - 自动清理：loop 失败/取消时，dirty worktree 保留供检查，clean worktree 自动删除
  - 用户明确确认后才合并回主分支
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WorktreeInfo:
    """Worktree 元数据。"""
    worktree_id: str
    path: Path
    base_branch: str
    base_commit: str
    is_dirty: bool = False
    auto_cleanup: bool = True


class WorktreeManager:
    """Git worktree 生命周期管理。"""

    def __init__(self, repo_root: str | Path | None = None):
        """初始化 WorktreeManager。

        Args:
            repo_root: Git 仓库根目录，默认为当前目录
        """
        self.repo_root = Path(repo_root) if repo_root else Path.cwd()
        self._validate_git_repo()

    def _validate_git_repo(self) -> None:
        """校验当前目录是否为 Git 仓库。"""
        try:
            subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.repo_root,
                capture_output=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise ValueError(f"不是 Git 仓库: {self.repo_root}")

    def _run_git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """运行 git 命令。"""
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=check,
        )

    def _get_current_branch(self) -> str:
        """获取当前分支名。"""
        result = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        return result.stdout.strip()

    def _get_current_commit(self) -> str:
        """获取当前 commit SHA。"""
        result = self._run_git("rev-parse", "HEAD")
        return result.stdout.strip()

    def _is_dirty(self) -> bool:
        """检查工作区是否有未提交改动。"""
        result = self._run_git("status", "--porcelain")
        return bool(result.stdout.strip())

    def create(self, worktree_id: str, require_clean_base: bool = False) -> WorktreeInfo:
        """创建隔离 worktree。

        Args:
            worktree_id: worktree 标识符（如 goal-123 或 loop-456）
            require_clean_base: 是否要求原工作区干净（无未提交改动）

        Returns:
            WorktreeInfo

        Raises:
            ValueError: 原工作区有未提交改动且 require_clean_base=True
            RuntimeError: git worktree 创建失败
        """
        # 检查原工作区状态
        if require_clean_base and self._is_dirty():
            raise ValueError(
                "原工作区有未提交改动，不能创建 worktree。"
                "请先提交或暂存改动，或设置 require_clean_base=False。"
            )

        base_branch = self._get_current_branch()
        base_commit = self._get_current_commit()

        # worktree 路径：data/worktrees/<worktree_id>
        worktree_path = self.repo_root / "data" / "worktrees" / worktree_id
        if worktree_path.exists():
            raise ValueError(f"Worktree 已存在: {worktree_path}")

        # 创建目录
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # 创建新分支并关联 worktree
        branch_name = f"worktree/{worktree_id}"
        try:
            self._run_git("worktree", "add", "-b", branch_name, str(worktree_path), base_commit)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"创建 worktree 失败: {exc.stderr}") from exc

        return WorktreeInfo(
            worktree_id=worktree_id,
            path=worktree_path,
            base_branch=base_branch,
            base_commit=base_commit,
            is_dirty=False,
        )

    def get_diff_summary(self, worktree: WorktreeInfo) -> str:
        """生成 worktree 的 diff 摘要。

        Args:
            worktree: WorktreeInfo

        Returns:
            diff 摘要文本（统计 + 文件列表）
        """
        if not worktree.path.exists():
            return f"Worktree 不存在: {worktree.path}"

        try:
            # 统计改动
            stat_result = self._run_git(
                "-C", str(worktree.path),
                "diff", "--stat", worktree.base_commit,
            )
            stat = stat_result.stdout.strip()

            # 文件列表
            files_result = self._run_git(
                "-C", str(worktree.path),
                "diff", "--name-status", worktree.base_commit,
            )
            files = files_result.stdout.strip()

            if not stat and not files:
                return "无改动"

            return f"改动统计:\n{stat}\n\n文件列表:\n{files}"
        except subprocess.CalledProcessError as exc:
            return f"获取 diff 失败: {exc.stderr}"

    def check_dirty(self, worktree: WorktreeInfo) -> bool:
        """检查 worktree 是否有未提交改动。"""
        if not worktree.path.exists():
            return False
        result = self._run_git(
            "-C", str(worktree.path),
            "status", "--porcelain",
            check=False,
        )
        return bool(result.stdout.strip())

    def remove(self, worktree: WorktreeInfo, force: bool = False) -> None:
        """删除 worktree。

        Args:
            worktree: WorktreeInfo
            force: 是否强制删除（即使有未提交改动）

        Raises:
            ValueError: worktree 有未提交改动且 force=False
            RuntimeError: git worktree 删除失败
        """
        if not worktree.path.exists():
            return  # 已删除，幂等

        # 检查是否有未提交改动
        if not force and self.check_dirty(worktree):
            raise ValueError(
                f"Worktree 有未提交改动: {worktree.path}\n"
                "若确认放弃改动，请使用 force=True"
            )

        try:
            # 删除 worktree
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(str(worktree.path))
            self._run_git(*args)

            # 删除分支
            branch_name = f"worktree/{worktree.worktree_id}"
            self._run_git("branch", "-D", branch_name, check=False)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"删除 worktree 失败: {exc.stderr}") from exc

    def merge_suggestion(self, worktree: WorktreeInfo) -> str:
        """生成合并建议（用户手动执行）。

        不自动合并，只返回命令供用户确认执行。

        Args:
            worktree: WorktreeInfo

        Returns:
            合并命令建议
        """
        branch_name = f"worktree/{worktree.worktree_id}"
        return (
            f"验证通过！建议合并步骤：\n"
            f"1. 切换到目标分支: git checkout {worktree.base_branch}\n"
            f"2. 合并改动: git merge {branch_name}\n"
            f"3. 推送（如需要）: git push\n"
            f"4. 清理 worktree: 使用 /loop clean 或手动 git worktree remove"
        )
