import json
import os
from pathlib import Path
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest
from PIL import Image

from app import attachments
from app.config.loader import use_workspace_root
from app.models.chatgpt_adapter import ChatGPTAdapter
from app.models.protocol import ToolResult
from app.storage import Storage
from app.tools.builtin.view_image import make_view_image_tool
from app.tools.registry import ToolRegistry
from app.util import process_sandbox


def test_view_image_returns_responses_image_without_storing_base64(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    Image.new("RGB", (8, 12), "red").save(workspace / "screenshot.png")
    monkeypatch.setattr(attachments, "DEFAULT_ATTACHMENT_ROOT", tmp_path / "attachments")
    registry = ToolRegistry()
    registry.register(make_view_image_tool("test-session", lambda: True))
    with use_workspace_root(workspace):
        assert "view_image" not in {tool["name"] for tool in registry.schemas_for_task("inspect code")}
        assert "view_image" in {tool["name"] for tool in registry.schemas_for_task("查看截图")}
        output, error = registry.execute("view_image", {"path": "screenshot.png"})
        assert not error
        assert registry.execute("view_image", {"path": "../outside.png"})[1]
    adapter = object.__new__(ChatGPTAdapter)
    adapter._cfg = SimpleNamespace(model="gpt-6.2-sol")
    messages = [
        {"type": "function_call", "call_id": "call_1", "name": "view_image", "arguments": "{}"},
        *adapter.format_tool_results([ToolResult("call_1", output)]),
    ]
    store = Storage(tmp_path / "test.db")
    try:
        store.create_session("test-session", "test", "codex")
        store.save_messages("test-session", messages)
        saved = store.load_messages("test-session")
    finally:
        store.close()
    assert "base64" not in json.dumps(saved)
    assert saved[1]["output"][1]["source"]["type"] == "file"
    result = adapter._params(saved, [], None, "medium")["input"][1]
    assert result["call_id"] == "call_1"
    assert result["output"][1]["type"] == "input_image"
    assert result["output"][1]["image_url"].startswith("data:image/png;base64,")
    remaining, removed = attachments.strip_image_blocks(saved)
    assert removed == 1 and remaining[1]["output"] == saved[1]["output"][:1]


def test_view_image_rejects_nonvisual_provider():
    registry = ToolRegistry()
    registry.register(make_view_image_tool("test-session", lambda: False))
    output, error = registry.execute("view_image", {"path": "unused.png"})
    assert error and "不支持" in output


def test_linux_workspace_sandbox_restricts_mounts_and_network(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLAB_PROCESS_SANDBOX", "workspace")
    monkeypatch.setattr(process_sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(Path, "is_file", lambda self: str(self) == "/usr/bin/bwrap")
    protected = tmp_path / ".git"
    protected.mkdir()
    with use_workspace_root(tmp_path):
        command = process_sandbox.sandbox_command(["bash", "-c", "true"])
    assert command[0] == "/usr/bin/bwrap"
    assert "--unshare-all" in command and "--disable-userns" in command
    assert command[command.index("--bind"):][:3] == ["--bind", str(tmp_path), str(tmp_path)]
    assert command[-7:] == ["--ro-bind", str(protected), str(protected), "--", "bash", "-c", "true"]


def test_requested_sandbox_fails_closed_on_unsupported_platform(monkeypatch):
    monkeypatch.setenv("AGENTLAB_PROCESS_SANDBOX", "read-only")
    monkeypatch.delenv("AGENTLAB_WSL_DISTRO", raising=False)
    monkeypatch.setattr(process_sandbox.platform, "system", lambda: "Windows")
    with pytest.raises(process_sandbox.ProcessSandboxError, match="command not started"):
        process_sandbox.sandbox_command(["powershell"])


def test_windows_wsl_sandbox_only_maps_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setenv("AGENTLAB_WSL_DISTRO", "AgentLab")
    (tmp_path / "System32").mkdir()
    (tmp_path / "System32" / "wsl.exe").touch()
    root = PureWindowsPath(r"C:\Projects\测试")
    command = process_sandbox._windows_sandbox(
        ["powershell", "-NoProfile", "-Command", "printf hello"],
        root, root / "src", [root / ".git"], "workspace",
    )
    assert command[1:5] == ["--distribution", "AgentLab", "--cd", "/"]
    assert "interop" in command[8]
    assert "--unshare-all" in command and "--clearenv" in command
    assert command[-12:] == [
        "--bind", "/mnt/c/Projects/测试", "/workspace", "--ro-bind",
        "/mnt/c/Projects/测试/.git", "/workspace/.git",
        "--chdir", "/workspace/src", "--", "/usr/bin/bash", "-c", "printf hello",
    ]
    with pytest.raises(process_sandbox.ProcessSandboxError, match="cwd"):
        process_sandbox._windows_sandbox(
            ["powershell", "-NoProfile", "-Command", "true"], root, root.parent, [], "read-only",
        )


@pytest.mark.skipif(os.name == "nt", reason="symlink creation may require privileges")
def test_workspace_sandbox_rejects_symlinked_protected_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLAB_PROCESS_SANDBOX", "workspace")
    (tmp_path / ".git").symlink_to(tmp_path / "other")
    with use_workspace_root(tmp_path), pytest.raises(process_sandbox.ProcessSandboxError, match="symlinked"):
        process_sandbox.sandbox_command(["sh"])


def test_shell_and_command_sessions_use_the_same_wrapper(tmp_path, monkeypatch):
    from app.tools.builtin import interactive, shell

    monkeypatch.setenv("AGENTLAB_PROCESS_SANDBOX", "read-only")
    monkeypatch.setattr(process_sandbox.platform, "system", lambda: "Windows")
    registry = ToolRegistry()
    registry.register(shell.SHELL)
    for tool in interactive.make_terminal_tools(interactive.PtySessionManager(str(tmp_path))):
        registry.register(tool)
    with use_workspace_root(tmp_path):
        for name in ("shell", "exec_command"):
            result, error = registry.execute(name, {"command": "echo blocked"}, approved_action=name)
            assert error and "command not started" in result
