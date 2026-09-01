"""离线测试:MCPManager。

两部分:
  1. _content_to_text 纯函数:文本/图片/错误块转换。
  2. sync↔async 桥:不连真 server,手动起后台 loop + 注入 fake async session,
     验证 call_tool 能把调用投递进 loop 并同步拿回结果,以及超时处理。
"""
import asyncio
import os
import time
import types
from pathlib import Path

import pytest

from app.mcp.config import MCPServerConfig
from app.mcp.manager import (
    MCPManager,
    _build_stdio_env,
    _content_to_text,
    _format_connect_error,
    _mcp_tool_input_schema,
    _playwright_channel_for_progid,
    _resolve_playwright_browser_args,
    _resolve_stdio_command,
    _resolve_stdio_launch,
    _resolve_stdio_cwd,
)


# ── _content_to_text ─────────────────────────────────────────────────────────

def _block(**kw):
    return types.SimpleNamespace(**kw)


def _result(content, is_error=False):
    return types.SimpleNamespace(content=content, isError=is_error)


def test_text_blocks_joined():
    r = _result([_block(type="text", text="hello"), _block(type="text", text="world")])
    text, is_error = _content_to_text(r)
    assert text == "hello\nworld"
    assert is_error is False


def test_image_block_omitted():
    r = _result([_block(type="image", mimeType="image/png", data="AAAA")])
    text, _ = _content_to_text(r)
    assert "image omitted" in text
    assert "image/png" in text


def test_error_flag_propagates():
    r = _result([_block(type="text", text="boom")], is_error=True)
    text, is_error = _content_to_text(r)
    assert is_error is True
    assert "boom" in text


def test_snake_case_error_flag_propagates():
    r = types.SimpleNamespace(
        content=[_block(type="text", text="boom")],
        is_error=True,
    )
    _, is_error = _content_to_text(r)
    assert is_error is True


def test_error_heading_is_treated_as_error_without_protocol_flag():
    r = _result([_block(type="text", text="### Error\nElement not found")])
    _, is_error = _content_to_text(r)
    assert is_error is True


def test_empty_content():
    text, is_error = _content_to_text(_result([]))
    assert text == "(empty result)"


def test_connect_error_expands_exception_group():
    error = ExceptionGroup("outer", [PermissionError("denied"), RuntimeError("boom")])

    text = _format_connect_error(error)

    assert "PermissionError: denied" in text
    assert "RuntimeError: boom" in text


def test_mcp_tool_info_accepts_sdk_v2_input_schema_name():
    tool = types.SimpleNamespace(
        name="browser_snapshot",
        description="snapshot",
        input_schema={"type": "object", "properties": {}},
    )

    assert _mcp_tool_input_schema(tool) == {"type": "object", "properties": {}}


def test_secret_redacted_in_result():
    r = _result([_block(type="text", text="key=sk-abcdefghijklmnopqrstuvwxyz0123")])
    text, _ = _content_to_text(r)
    assert "sk-abcdefghij" not in text
    assert "sk-***" in text


# ── stdio 跨平台启动 ──────────────────────────────────────────────────────────

def test_windows_resolves_npx_cmd(monkeypatch):
    seen = []

    def fake_which(command):
        seen.append(command)
        if command == "npx.cmd":
            return r"C:\Program Files\nodejs\npx.cmd"
        return None

    monkeypatch.setattr("app.mcp.manager.shutil.which", fake_which)

    resolved = _resolve_stdio_command("npx", system="Windows")

    assert resolved == r"C:\Program Files\nodejs\npx.cmd"
    assert seen[0] == "npx.cmd"


def test_windows_expands_npx_cmd_to_node_cli(monkeypatch, tmp_path):
    node_root = tmp_path / "nodejs"
    cli = node_root / "node_modules" / "npm" / "bin" / "npx-cli.js"
    cli.parent.mkdir(parents=True)
    (node_root / "node.exe").write_bytes(b"")
    cli.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "app.mcp.manager._resolve_stdio_command",
        lambda command, system=None: str(node_root / "npx.cmd"),
    )

    command, args = _resolve_stdio_launch(
        "npx", ["-y", "@playwright/mcp@latest"], system="Windows",
    )

    assert command == str(node_root / "node.exe")
    assert args == [str(cli), "-y", "@playwright/mcp@latest"]


@pytest.mark.parametrize(("progid", "channel"), [
    ("MSEdgeHTM", "msedge"),
    ("ChromeHTML", "chrome"),
    ("FirefoxURL-308046B0AF4A39CB", "firefox"),
])
def test_maps_windows_default_browser_progid(progid, channel):
    assert _playwright_channel_for_progid(progid) == channel


def test_resolves_system_default_browser_argument(monkeypatch):
    monkeypatch.setattr(
        "app.mcp.manager._windows_default_browser_progid",
        lambda: "MSEdgeHTM",
    )

    args = _resolve_playwright_browser_args(
        ["-y", "@playwright/mcp@latest", "--browser", "system-default"],
        system="Windows",
    )

    assert args[-2:] == ["--browser", "msedge"]


def test_windows_keeps_explicit_executable_extension(monkeypatch):
    monkeypatch.setattr(
        "app.mcp.manager.shutil.which",
        lambda command: command if command == "custom.cmd" else None,
    )

    assert _resolve_stdio_command("custom.cmd", system="Windows") == "custom.cmd"


def test_missing_npx_has_windows_install_hint(monkeypatch):
    monkeypatch.setattr("app.mcp.manager.shutil.which", lambda command: None)

    with pytest.raises(FileNotFoundError, match="Node.js LTS"):
        _resolve_stdio_command("npx", system="Windows")


def test_windows_stdio_env_includes_runtime_paths_and_allowlist(monkeypatch):
    monkeypatch.setattr(
        os,
        "environ",
        {
            "PATH": r"C:\Windows\System32",
            "SYSTEMROOT": r"C:\Windows",
            "COMSPEC": r"C:\Windows\System32\cmd.exe",
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "TEMP": r"C:\Temp",
            "USERPROFILE": r"C:\Users\tester",
            "APPDATA": r"C:\Users\tester\AppData\Roaming",
            "LOCALAPPDATA": r"C:\Users\tester\AppData\Local",
            "MCP_TEST_TOKEN": "allowed",
            "UNLISTED_SECRET": "must-not-leak",
        },
    )
    server = MCPServerConfig(name="playwright", env_allowlist=["MCP_TEST_TOKEN"])

    env = _build_stdio_env(server, system="Windows")

    assert env["SYSTEMROOT"] == r"C:\Windows"
    assert env["PATHEXT"] == ".COM;.EXE;.BAT;.CMD"
    assert env["LOCALAPPDATA"].endswith(r"AppData\Local")
    assert env["MCP_TEST_TOKEN"] == "allowed"
    assert "UNLISTED_SECRET" not in env


def test_stdio_cwd_is_anchored_to_project_root():
    resolved = Path(_resolve_stdio_cwd("data/browser-profiles/test"))

    assert resolved.is_absolute()
    assert resolved.parts[-3:] == ("data", "browser-profiles", "test")


# ── sync↔async 桥 ────────────────────────────────────────────────────────────

class FakeSession:
    """假 async session:call_tool 是协程,按需 sleep 模拟慢调用。"""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        return _result([_block(type="text", text=f"called {name}")])


@pytest.fixture
def running_manager():
    """起一个只有后台 loop、注入 fake session 的 manager;测完 stop。"""
    mgr = MCPManager(servers=[])
    mgr._ensure_loop()
    yield mgr
    # 注入式测试没有真实 _serve task,直接停 loop 即可
    if mgr.loop is not None:
        mgr.loop.call_soon_threadsafe(mgr.loop.stop)
        if mgr.thread:
            mgr.thread.join(timeout=5)


def test_call_tool_bridges_into_loop(running_manager):
    mgr = running_manager
    mgr._sessions["playwright"] = FakeSession()
    text, is_error = mgr.call_tool("playwright", "browser_navigate", {"url": "x"})
    assert is_error is False
    assert text == "called browser_navigate"


def test_call_tool_unknown_server(running_manager):
    text, is_error = running_manager.call_tool("nope", "x", {})
    assert is_error is True
    assert "not connected" in text


def test_call_tool_timeout(running_manager):
    mgr = running_manager
    mgr._sessions["slow"] = FakeSession(delay=2.0)
    text, is_error = mgr.call_tool("slow", "browser_wait", {}, timeout=0.2)
    assert is_error is True
    assert "timeout" in text


def test_start_noop_without_servers():
    """没有配置 server 时 start() 不应起 loop。"""
    mgr = MCPManager(servers=[])
    mgr.start()
    assert mgr.loop is None


def test_stop_idempotent_without_loop():
    """没起过 loop 时 stop() 应安全返回。"""
    MCPManager(servers=[MCPServerConfig(name="x")]).stop()  # 不抛异常即可
