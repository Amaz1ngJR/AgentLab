"""Reviewed worktree delivery without committing, stashing or overwriting parent edits."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import stat
from pathlib import Path

from app.agent.approval import request_tool_approval
from app.agent.cancel import Cancelled, CancelToken
from app.tools.registry import Tool, ToolExecutionError
from app.workspace.worktree import WorktreeManager


def _approve_delivery(approval, tool, action, args, cancel: CancelToken, *, phase: str) -> None:
    cancel.raise_if_cancelled()
    decision = request_tool_approval(approval, tool, action, args)
    if decision.cancelled or (not decision.approved and decision.feedback):
        reason = f"{phase}_approval_cancelled"
        cancel.cancel(reason)
        raise Cancelled(decision.feedback or f"{phase} approval cancelled",
                        reason="budget_exhausted" if cancel.reason == "budget_exhausted" else reason)
    if not decision.approved:
        raise ToolExecutionError(f"{phase} approval denied")
    cancel.raise_if_cancelled()


def scope_state(workspace: Path, scopes: tuple[str, ...]) -> dict[str, str]:
    """Fingerprint scoped working files, independently of Git/index and other writers."""
    workspace = workspace.resolve()
    state = {}
    for scope in scopes:
        root = workspace / scope
        if not root.resolve().is_relative_to(workspace):
            raise ToolExecutionError("write scope escapes workspace")
        paths = [root] if root.is_symlink() or not root.is_dir() else root.rglob("*")
        for path in paths:
            relative = path.relative_to(workspace)
            if any(part.casefold() == ".git" for part in relative.parts):
                continue
            if path.is_symlink():
                data = b"symlink:" + os.fsencode(os.readlink(path))
            elif path.is_file():
                data = b"file:" + path.read_bytes()
            else:
                continue
            mode = stat.S_IMODE(path.lstat().st_mode)
            state[relative.as_posix()] = hashlib.sha256(str(mode).encode() + b":" + data).hexdigest()
    return state


def _integrate_shared(result, *, workspace, tools, approval, checks, cancel, file_scope,
                      adopt_current, check_timeout_seconds):
    """Shared writes already exist: verify their frozen state, never replay a patch."""
    from app.config.loader import use_workspace_root
    original = result.metrics.get("shared_artifact")
    if not file_scope or original is None:
        raise ToolExecutionError("shared workspace artifact is missing; review required")
    current = scope_state(workspace, file_scope)
    expected = current if adopt_current else original
    if current != expected:
        raise ToolExecutionError("shared workspace artifact changed; review required")
    shell = tools.get("shell")
    if shell is None:
        raise ToolExecutionError("verification requires an allowed shell tool")
    evidence = []
    for command in checks:
        cancel.raise_if_cancelled()
        args = {"command": command, "cwd": str(workspace), "timeout": check_timeout_seconds}
        with use_workspace_root(workspace):
            action = shell.approval_action(args)
            if action:
                _approve_delivery(approval, shell, action, args, cancel, phase="verification")
            output, error = tools.execute("shell", args, approved_action=action, cancel=cancel)
        evidence.append({"command": command, "output": output[:4000], "error": error})
        if scope_state(workspace, file_scope) != expected:
            raise ToolExecutionError("verification changed the deliverable; review required")
        if error:
            return {"status": "verification_failed", "workspace_mode": "shared", "evidence": evidence}
    cancel.raise_if_cancelled()
    if scope_state(workspace, file_scope) != expected:
        raise ToolExecutionError("shared workspace artifact changed; review required")
    artifact_sha = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
    status = "parent_verified" if adopt_current else "integrated"
    if adopt_current:
        changed_since_child = sorted(path for path in original.keys() | expected.keys()
                                     if original.get(path) != expected.get(path))
        review = Tool("integrate_agent", "Accept the currently verified shared workspace scope", {},
                      lambda args: "", risk="write", requires_approval=True)
        args = {"adopt_current": True, "subagent_id": result.subagent_id,
                "workspace": str(workspace), "file_scope": list(file_scope),
                "changed_since_child": changed_since_child, "artifact_sha256": artifact_sha,
                "child_execution_status": result.status, "checks": checks,
                "check_timeout_seconds": check_timeout_seconds}
        _approve_delivery(approval, review, "integrate_agent", args, cancel, phase="integration")
        if scope_state(workspace, file_scope) != expected:
            raise ToolExecutionError("approved shared workspace artifact changed; revalidation required")
    result.delivery_status = status
    return {"status": status, "workspace_mode": "shared", "artifact_sha256": artifact_sha,
            "changed_files": result.changed_files,
            "verification": evidence, **({"file_scope": list(file_scope),
                                           "changed_since_child": changed_since_child} if adopt_current else {})}


def patch_bytes(manager: WorktreeManager, worktree) -> bytes:
    """Temporary index includes untracked files without changing the real index."""
    fd, index = tempfile.mkstemp(prefix="agentlab-index-")
    os.close(fd)
    os.unlink(index)
    env = {**os.environ, "GIT_INDEX_FILE": index}
    try:
        def git(*args):
            return subprocess.run(["git", *args], cwd=worktree.path, env=env,
                                  capture_output=True, check=True).stdout
        git("read-tree", worktree.base_commit)
        git("add", "-A", "--", ".")
        return git("diff", "--cached", "--binary", "--no-ext-diff", worktree.base_commit)
    finally:
        Path(index).unlink(missing_ok=True)
        Path(index + ".lock").unlink(missing_ok=True)


def _check_parent_paths(manager: WorktreeManager, result, changed: list[str]) -> None:
    """Allow unrelated edits, but never overwrite staged, tracked or new parent files."""
    if manager.current_commit() != (result.worktree.parent_commit or result.worktree.base_commit):
        raise ToolExecutionError("parent workspace drifted: HEAD changed; review and rebase required")
    paths = [f":(literal){path}" for path in changed]
    if not paths:
        raise ToolExecutionError("no patch to integrate")
    if result.worktree.parent_commit is not None:
        # Baseline contains parent unstaged/untracked content, not merely HEAD/index.
        current_tree = manager.snapshot_tree()
        diff = manager._run_git("diff", "--quiet", result.worktree.base_commit,
                                current_tree, "--", *paths, check=False)
        if diff.returncode:
            raise ToolExecutionError("parent workspace drifted in child paths; review required")
        if result.worktree.parent_index_tree:
            index_tree = manager._run_git("write-tree").stdout.strip()
            diff = manager._run_git("diff", "--quiet", result.worktree.parent_index_tree,
                                    index_tree, "--", *paths, check=False)
            if diff.returncode:
                raise ToolExecutionError("parent workspace drifted in child paths; index changed")
        return
    for flags in ([], ["--cached"]):
        diff = subprocess.run(
            ["git", "diff", "--quiet", "--no-ext-diff", "--no-textconv", *flags,
             result.worktree.base_commit, "--", *paths],
            cwd=manager.repo_root, capture_output=True,
        )
        if diff.returncode:
            raise ToolExecutionError(
                "parent workspace drifted in child paths; review and rebase required")
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", *paths],
        cwd=manager.repo_root, capture_output=True, check=True,
    )
    if untracked.stdout:
        raise ToolExecutionError("parent workspace drifted: child paths already exist; review required")


def integrate_result(result, *, workspace: Path, tools, approval, checks: list[str], cancel: CancelToken,
                     file_scope: tuple[str, ...] = (), adopt_current: bool = False,
                     check_timeout_seconds: int = 60):
    """Verify exact patch, approve exact artifact, revalidate baseline, then apply once."""
    from app.config.loader import use_workspace_root
    if result.delivery_status in {"integrated", "parent_verified"}:
        return {"status": result.delivery_status, "already_integrated": True}
    if adopt_current and (result.worktree is not None or result.delivery_status not in {"applied_unverified", "no_changes"}):
        raise ToolExecutionError("adopt_current requires a stopped shared write result with a recorded scope")
    if not adopt_current and result.status != "succeeded":
        raise ToolExecutionError("only successful write results may be integrated")
    if not checks or any(not command.strip() for command in checks):
        raise ToolExecutionError("explicit verification commands are required")
    if type(check_timeout_seconds) is not int or not 1 <= check_timeout_seconds <= 600:
        raise ToolExecutionError("check_timeout_seconds must be an integer from 1 to 600")
    if result.worktree is None:
        if not adopt_current and result.delivery_status != "applied_unverified":
            raise ToolExecutionError("only successful write results may be integrated")
        return _integrate_shared(result, workspace=workspace, tools=tools, approval=approval,
                                 checks=checks, cancel=cancel, file_scope=file_scope, adopt_current=adopt_current,
                                 check_timeout_seconds=check_timeout_seconds)
    manager = WorktreeManager(workspace)
    review_baseline = manager.state_fingerprint()
    _check_parent_paths(manager, result, manager.changed_files(result.worktree))
    shell = tools.get("shell")
    if shell is None:
        raise ToolExecutionError("verification requires an allowed shell tool")
    # Freeze the deliverable before validation. A successful command that rewrites
    # source files has not validated the artifact that would be applied.
    patch = patch_bytes(manager, result.worktree)
    evidence = []
    for command in checks:
        cancel.raise_if_cancelled()
        args = {"command": command, "cwd": str(result.worktree.path), "timeout": check_timeout_seconds}
        with use_workspace_root(result.worktree.path):
            action = shell.approval_action(args)
            if action:
                _approve_delivery(approval, shell, action, args, cancel, phase="verification")
            output, error = tools.execute("shell", args, approved_action=action, cancel=cancel)
        evidence.append({"command": command, "output": output[:4000], "error": error})
        if error:
            return {"status": "verification_failed", "evidence": evidence}
    cancel.raise_if_cancelled()
    if patch_bytes(manager, result.worktree) != patch:
        raise ToolExecutionError("verification changed the deliverable; rerun validation on the reviewed artifact")
    if not patch:
        raise ToolExecutionError("no patch to integrate")
    # Scope is checked against actual paths, including verification side effects.
    changed = manager.changed_files(result.worktree)
    if file_scope and any(not any(Path(path) == Path(scope) or Path(path).is_relative_to(scope)
                                  for scope in file_scope) for path in changed):
        raise ToolExecutionError("verification changed files outside approved child scope")
    _check_parent_paths(manager, result, changed)
    sha = hashlib.sha256(patch).hexdigest()
    approval_tool = Tool("integrate_agent", "Apply reviewed child patch to parent workspace", {},
                         lambda args: "", risk="write", requires_approval=True)
    args = {"subagent_id": result.subagent_id, "workspace": str(workspace),
            "changed_files": changed, "patch_sha256": sha,
            "diff": patch.decode("utf-8", errors="replace"),
            "verification": evidence}
    _approve_delivery(approval, approval_tool, "integrate_agent", args, cancel, phase="integration")
    if (manager.state_fingerprint() != review_baseline
            or patch_bytes(manager, result.worktree) != patch):
        raise ToolExecutionError("workspace or approved patch changed; revalidation required")
    subprocess.run(["git", "apply", "--check", "--binary", "-"], cwd=workspace,
                   input=patch, capture_output=True, check=True)
    # git apply is all-or-nothing for normal hunks (no --reject); never uses --unsafe-paths.
    subprocess.run(["git", "apply", "--binary", "-"], cwd=workspace,
                   input=patch, capture_output=True, check=True)
    result.delivery_status = "integrated"
    result.changed_files = changed
    return {"status": "integrated", "patch_sha256": sha, "changed_files": changed,
            "verification": evidence}
