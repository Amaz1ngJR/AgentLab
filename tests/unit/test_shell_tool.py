"""离线测试：shell 工具。mock subprocess.run 验证 cwd 锁定 / 超时 / 输出格式。"""
from unittest.mock import MagicMock, patch

import subprocess

from app.tools.builtin.shell import (
    DEFAULT_TIMEOUT,
    MAX_OUTPUT_BYTES,
    SHELL,
    _build_argv,
    _run_shell,
)
from app.tools.registry import ToolRegistry


def _make_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    """构造 subprocess.run 返回的 CompletedProcess mock。"""
    cp = MagicMock()
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = returncode
    return cp


def test_build_argv_unix(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    argv = _build_argv("ls -la")
    assert argv == ["bash", "-c", "ls -la"]


def test_build_argv_windows(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Windows")
    argv = _build_argv("Get-ChildItem")
    assert argv[:3] == ["powershell", "-NoProfile", "-Command"]
    assert argv[3] == "Get-ChildItem"


def test_run_shell_success_includes_stdout_and_exit_code(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return _make_completed(stdout="hello\nworld\n", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = _run_shell({"command": "echo hello && echo world"})

    assert "hello" in out and "world" in out
    assert "[exit code: 0]" in out
    # cwd 应该被设到 workspace
    assert captured["cwd"] == str(tmp_path.resolve())
    # timeout 默认 30
    assert captured["timeout"] == DEFAULT_TIMEOUT


def test_run_shell_includes_stderr_on_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    def fake_run(*args, **kwargs):
        return _make_completed(stdout="", stderr="bash: nope: command not found", returncode=127)

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = _run_shell({"command": "nope"})

    assert "[stderr]" in out
    assert "command not found" in out
    assert "[exit code: 127]" in out


def test_run_shell_custom_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return _make_completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    _run_shell({"command": "sleep 5", "timeout": 60})
    assert captured["timeout"] == 60


def test_shell_outside_cwd_requires_separate_approval(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    registry = ToolRegistry()
    registry.register(SHELL)
    args = {"command": "pwd", "cwd": str(outside)}

    denied, is_error = registry.execute(
        "shell",
        args,
        approved_action="shell",
    )
    assert is_error is True
    assert denied == "approval required: shell_outside_workspace"

    with patch("subprocess.run", return_value=_make_completed()) as run:
        output, is_error = registry.execute(
            "shell",
            args,
            approved_action="shell_outside_workspace",
        )

    assert is_error is False
    assert "[exit code: 0]" in output
    assert run.call_args.kwargs["cwd"] == str(outside.resolve())


def test_run_shell_timeout_returns_error(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="bash", timeout=30)

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = _run_shell({"command": "sleep 999"})
    assert out.startswith("timeout:")
    assert "30s" in out


def test_run_shell_truncates_long_output(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    huge = "a" * (MAX_OUTPUT_BYTES * 2)

    def fake_run(*args, **kwargs):
        return _make_completed(stdout=huge, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = _run_shell({"command": "yes"})
    assert "truncated" in out
    # 截断后总长度应该接近 MAX_OUTPUT_BYTES + 截断说明
    assert len(out) < MAX_OUTPUT_BYTES + 200


def test_run_shell_empty_command_refused():
    out = _run_shell({"command": ""})
    assert out.startswith("refused:")


def test_shell_tool_requires_approval():
    """shell 是高风险工具,必须默认走审批。"""
    assert SHELL.requires_approval is True
    assert SHELL.name == "shell"
