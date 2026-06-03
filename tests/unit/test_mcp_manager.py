"""离线测试:MCPManager。

两部分:
  1. _content_to_text 纯函数:文本/图片/错误块转换。
  2. sync↔async 桥:不连真 server,手动起后台 loop + 注入 fake async session,
     验证 call_tool 能把调用投递进 loop 并同步拿回结果,以及超时处理。
"""
import asyncio
import time
import types

import pytest

from app.mcp.config import MCPServerConfig
from app.mcp.manager import MCPManager, _content_to_text


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


def test_empty_content():
    text, is_error = _content_to_text(_result([]))
    assert text == "(empty result)"


def test_secret_redacted_in_result():
    r = _result([_block(type="text", text="key=sk-abcdefghijklmnopqrstuvwxyz0123")])
    text, _ = _content_to_text(r)
    assert "sk-abcdefghij" not in text
    assert "sk-***" in text


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
