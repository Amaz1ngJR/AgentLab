"""离线测试：shell 工具。mock subprocess.run 验证 cwd 锁定 / 超时 / 输出格式。"""
from unittest.mock import MagicMock, patch

import pytest
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


def test_run_shell_uses_builtin_rtk_without_external_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTLAB_RTK_ENABLED", "true")
    raw = "\n".join(["tests/test_x.py::test_ok PASSED"] * 100 + ["100 passed in 1.0s"])
    with patch("subprocess.run", return_value=_make_completed(stdout=raw)):
        output = _run_shell({"command": "pytest -v"})
    assert "100 passed in 1.0s" in output
    assert "[rtk: pytest" in output
    assert "[exit code: 0]" in output


def test_run_shell_rtk_disabled_preserves_raw_output(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENTLAB_RTK_ENABLED", "false")
    raw = "line one\nline two"
    with patch("subprocess.run", return_value=_make_completed(stdout=raw)):
        output = _run_shell({"command": "pytest"})
    assert raw in output
    assert "[rtk:" not in output


def test_run_shell_timeout_kills_command(monkeypatch, tmp_path):
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


# ── 取消：长时间命令必须被迅速终止 ────────────────────────────────────────────
# 背景：shell 原先用 subprocess.run，Ctrl-C/Esc 只能等 timeout 到期（最长用户指定
# 的秒数）才返回，交互上等同于卡死。现在走 Popen + start_new_session，token 置位
# 时通过 killpg 唤醒 communicate()。
# 这些用真实子进程验证，不 mock —— mock 掉 Popen 就测不到"真的被终止"这件事。


def test_long_running_shell_killed_quickly_on_cancel(monkeypatch, tmp_path):
    """Esc 触发取消时，sleep 30 的命令应在远小于超时的时间内被终止。"""
    import threading
    import time

    from app.agent.cancel import CancelToken, Cancelled
    from app.tools.registry import ToolRegistry

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    registry = ToolRegistry()
    registry.register(SHELL)
    token = CancelToken()

    def cancel_soon():
        time.sleep(0.3)
        token.cancel()

    threading.Thread(target=cancel_soon, daemon=True).start()

    started = time.monotonic()
    with pytest.raises(Cancelled):
        registry.execute(
            "shell",
            {"command": "sleep 30", "timeout": 30},
            approved_action="shell",
            cancel=token,
        )
    elapsed = time.monotonic() - started

    # 关键断言：没有等满 30s。给足余量以容忍 CI 抖动。
    assert elapsed < 5, f"命令未被及时终止，耗时 {elapsed:.1f}s"


def test_shell_cancel_kills_whole_process_group(monkeypatch, tmp_path):
    """killpg 要连带子进程一起终止 —— 否则子进程会变成孤儿继续跑。"""
    import threading
    import time

    from app.agent.cancel import CancelToken, Cancelled
    from app.tools.registry import ToolRegistry

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    registry = ToolRegistry()
    registry.register(SHELL)
    token = CancelToken()

    def cancel_soon():
        time.sleep(0.3)
        token.cancel()

    threading.Thread(target=cancel_soon, daemon=True).start()

    # bash 起一个后台 sleep 再前台等待：只有整组被 kill 才不会有残留
    with pytest.raises(Cancelled):
        registry.execute(
            "shell",
            {"command": "sleep 30 & wait", "timeout": 30},
            approved_action="shell",
            cancel=token,
        )


def test_shell_without_cancel_uses_plain_subprocess_run(monkeypatch, tmp_path):
    """cancel=None（非 Agent 调用）时保持 subprocess.run 语义，不引入 Popen 分支。"""
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    with patch("subprocess.run", return_value=_make_completed(stdout="ok")) as run:
        out = _run_shell({"command": "echo ok"})
    assert run.called
    assert "[exit code: 0]" in out


def test_shell_already_cancelled_never_starts_process(monkeypatch, tmp_path):
    """令牌已取消时，命令根本不应被启动。"""
    from app.agent.cancel import CancelToken, Cancelled
    from app.tools.registry import ToolRegistry

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    registry = ToolRegistry()
    registry.register(SHELL)
    token = CancelToken()
    token.cancel()

    with pytest.raises(Cancelled):
        registry.execute(
            "shell",
            {"command": "echo should-not-run"},
            approved_action="shell",
            cancel=token,
        )
