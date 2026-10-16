"""Reviewed worktree delivery without committing, stashing or overwriting parent edits."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from app.agent.approval import request_tool_approval
from app.agent.cancel import CancelToken
from app.tools.registry import Tool, ToolExecutionError
from app.workspace.worktree import WorktreeManager


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


def integrate_result(result, *, workspace: Path, tools, approval, checks: list[str], cancel: CancelToken,
                     file_scope: tuple[str, ...] = ()):
    """Verify exact patch, approve exact artifact, revalidate baseline, then apply once."""
    from app.config.loader import use_workspace_root
    if result.delivery_status == "integrated":
        return {"status": "integrated", "already_integrated": True}
    if result.status != "succeeded" or result.worktree is None:
        raise ToolExecutionError("only successful write results may be integrated")
    if not checks or any(not command.strip() for command in checks):
        raise ToolExecutionError("explicit verification commands are required")
    manager = WorktreeManager(workspace)
    if manager.state_fingerprint() != result.base_state_fingerprint:
        raise ToolExecutionError("parent workspace drifted; review and rebase required")
    shell = tools.get("shell")
    if shell is None:
        raise ToolExecutionError("verification requires an allowed shell tool")
    # Freeze the deliverable before validation. A successful command that rewrites
    # source files has not validated the artifact that would be applied.
    patch = patch_bytes(manager, result.worktree)
    evidence = []
    for command in checks:
        cancel.raise_if_cancelled()
        args = {"command": command, "cwd": str(result.worktree.path), "timeout": 60}
        with use_workspace_root(result.worktree.path):
            action = shell.approval_action(args)
            if action:
                decision = request_tool_approval(approval, shell, action, args)
                if decision.cancelled or decision.feedback:
                    from app.agent.cancel import Cancelled
                    raise Cancelled(decision.feedback or "verification approval cancelled")
                if not decision.approved:
                    raise ToolExecutionError("verification approval denied")
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
    sha = hashlib.sha256(patch).hexdigest()
    approval_tool = Tool("integrate_agent", "Apply reviewed child patch to parent workspace", {},
                         lambda args: "", risk="write", requires_approval=True)
    args = {"subagent_id": result.subagent_id, "workspace": str(workspace),
            "changed_files": changed, "patch_sha256": sha,
            "diff": patch.decode("utf-8", errors="replace"),
            "verification": evidence}
    decision = request_tool_approval(approval, approval_tool, "integrate_agent", args)
    if decision.cancelled or decision.feedback:
        from app.agent.cancel import Cancelled
        raise Cancelled(decision.feedback or "integration approval cancelled")
    if not decision.approved:
        raise ToolExecutionError("integration approval denied")
    cancel.raise_if_cancelled()
    if (manager.state_fingerprint() != result.base_state_fingerprint
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
