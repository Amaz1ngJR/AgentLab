"""离线测试：通用交互式终端会话(PtySession / PtySessionManager / terminal_* 工具)。

用真实 PTY 跑一个 *fake 交互式程序*(一个简单的 Python REPL 风格脚本:打印提示符、
读一行、回显结果、循环),验证驱动逻辑对任意交互式程序通用 —— 不绑定 vsm。
不依赖网络、不碰真实设备。
"""
import sys
import textwrap

import pytest

from app.tools.builtin import interactive as I
from app.tools.builtin.interactive import (
    MAX_OUTPUT_BYTES,
    PtySession,
    PtySessionManager,
    _normalize,
    _strip_ansi,
    make_terminal_tools,
)


# 一个最小交互式程序:打印 banner + 提示符,读一行,echo 回去,循环;quit 退出。
_FAKE_REPL = textwrap.dedent('''
    import sys
    print("FAKE-REPL ready")
    while True:
        sys.stdout.write(">>> ")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line or line.strip() == "quit":
            print("bye")
            break
        sys.stdout.write("echo: " + line.strip() + "\\n")
        sys.stdout.flush()
''')


@pytest.fixture
def repl_cmd(tmp_path):
    """把 fake REPL 写到临时文件,返回启动它的命令字符串。"""
    script = tmp_path / "fake_repl.py"
    script.write_text(_FAKE_REPL, encoding="utf-8")
    return f"{sys.executable} {script}"


# ── 纯函数 ────────────────────────────────────────────────────────────────────

def test_strip_ansi():
    assert _strip_ansi("\x1b[1;32mroot\x1b[m@host") == "root@host"


def test_normalize_crlf():
    assert _normalize(b"a\r\nb\r\n") == "a\nb\n"


# ── PtySession 直接驱动 ───────────────────────────────────────────────────────

def test_session_open_reads_banner(repl_cmd):
    import shlex
    s = PtySession("t1", shlex.split(repl_cmd))
    try:
        out = s.read_until_idle(idle=0.3, timeout=10)
        assert "FAKE-REPL ready" in out
        assert ">>>" in out
        assert s.alive
    finally:
        s.close()


def test_session_send_and_read(repl_cmd):
    import shlex
    s = PtySession("t1", shlex.split(repl_cmd))
    try:
        s.read_until_idle(idle=0.3, timeout=10)   # 吃掉 banner
        s.send("hello")
        out = s.read_until_idle(idle=0.3, timeout=10)
        assert "echo: hello" in out
    finally:
        s.close()


def test_session_close_kills_process(repl_cmd):
    import shlex
    s = PtySession("t1", shlex.split(repl_cmd))
    s.read_until_idle(idle=0.3, timeout=10)
    assert s.alive
    s.close()
    assert not s.alive


# ── PtySessionManager ─────────────────────────────────────────────────────────

def test_manager_open_get_close(repl_cmd):
    m = PtySessionManager()
    try:
        sid, session = m.open(repl_cmd)
        assert sid == "term-1"
        assert m.get(sid) is session
        assert len(m.list()) == 1
        assert m.close(sid)
        assert m.get(sid) is None
    finally:
        m.close_all()


def test_manager_empty_command_raises():
    m = PtySessionManager()
    with pytest.raises(ValueError):
        m.open("   ")


def test_manager_close_all(repl_cmd):
    m = PtySessionManager()
    m.open(repl_cmd)
    m.open(repl_cmd)
    assert len(m.list()) == 2
    m.close_all()
    assert m.list() == []


def test_manager_stop_alias(repl_cmd):
    # AgentSession.close 找 .stop;应等价于 close_all
    m = PtySessionManager()
    m.open(repl_cmd)
    m.stop()
    assert m.list() == []


# ── terminal_* 工具 ───────────────────────────────────────────────────────────

def _tools(manager):
    return {t.name: t for t in make_terminal_tools(manager)}


def test_terminal_open_returns_session_id_and_banner(repl_cmd):
    m = PtySessionManager()
    try:
        tools = _tools(m)
        out = tools["terminal_open"].executor({"command": repl_cmd, "timeout": 10})
        assert "[session term-1]" in out
        assert "FAKE-REPL ready" in out
    finally:
        m.close_all()


def test_terminal_open_empty_command_refused():
    m = PtySessionManager()
    out = _tools(m)["terminal_open"].executor({"command": ""})
    assert out.startswith("refused:")


def test_terminal_send_roundtrip(repl_cmd):
    m = PtySessionManager()
    try:
        tools = _tools(m)
        tools["terminal_open"].executor({"command": repl_cmd, "timeout": 10})
        out = tools["terminal_send"].executor(
            {"session_id": "term-1", "input": "world", "timeout": 10})
        assert "echo: world" in out
    finally:
        m.close_all()


def test_terminal_send_unknown_session():
    m = PtySessionManager()
    out = _tools(m)["terminal_send"].executor({"session_id": "nope", "input": "x"})
    assert out.startswith("refused:")
    assert "nope" in out


def test_terminal_send_detects_session_end(repl_cmd):
    m = PtySessionManager()
    try:
        tools = _tools(m)
        tools["terminal_open"].executor({"command": repl_cmd, "timeout": 10})
        # quit 让 fake REPL 退出
        out = tools["terminal_send"].executor(
            {"session_id": "term-1", "input": "quit", "timeout": 10})
        assert "bye" in out
        assert "已结束" in out
    finally:
        m.close_all()


def test_terminal_list(repl_cmd):
    m = PtySessionManager()
    try:
        tools = _tools(m)
        assert "无活跃" in tools["terminal_list"].executor({})
        tools["terminal_open"].executor({"command": repl_cmd, "timeout": 10})
        listing = tools["terminal_list"].executor({})
        assert "term-1" in listing
        assert "alive" in listing
    finally:
        m.close_all()


def test_terminal_close_tool(repl_cmd):
    m = PtySessionManager()
    tools = _tools(m)
    tools["terminal_open"].executor({"command": repl_cmd, "timeout": 10})
    out = tools["terminal_close"].executor({"session_id": "term-1"})
    assert "已关闭" in out
    assert tools["terminal_close"].executor({"session_id": "term-1"}).startswith("未知")


# ── 工具元信息 ────────────────────────────────────────────────────────────────

def test_tool_approval_flags():
    tools = _tools(PtySessionManager())
    assert tools["terminal_open"].requires_approval is True
    assert tools["terminal_send"].requires_approval is True
    # 关闭/列举无副作用,不需审批
    assert tools["terminal_close"].requires_approval is False
    assert tools["terminal_list"].requires_approval is False


def test_send_output_truncation(monkeypatch, repl_cmd):
    m = PtySessionManager()
    try:
        tools = _tools(m)
        tools["terminal_open"].executor({"command": repl_cmd, "timeout": 10})
        # 让 read_until_idle 返回超长内容,验证 _clip 截断
        huge = "x" * (MAX_OUTPUT_BYTES * 2)
        monkeypatch.setattr(I.PtySession, "read_until_idle", lambda self, **kw: huge)
        out = tools["terminal_send"].executor({"session_id": "term-1", "input": "x"})
        assert "truncated" in out
    finally:
        m.close_all()
