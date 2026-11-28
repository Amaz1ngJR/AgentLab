"""离线测试：通用交互式终端会话(PtySession / PtySessionManager / terminal_* 工具)。

用真实 PTY 跑一个 *fake 交互式程序*(一个简单的 Python REPL 风格脚本:打印提示符、
读一行、回显结果、循环),验证驱动逻辑对任意交互式程序通用 —— 不绑定 vsm。
不依赖网络、不碰真实设备。
"""
import sys
import shlex
import json
import os
import threading
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
    if os.name == "nt":
        pytest.skip("legacy terminal_open requires a Unix PTY")
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


@pytest.mark.skipif(os.name == "nt", reason="legacy terminal_open requires a Unix PTY")
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


@pytest.mark.skipif(os.name == "nt", reason="legacy terminal_open requires a Unix PTY")
def test_terminal_open_waits_through_idle_and_reports_exit_code():
    source = "import time; print('start'); time.sleep(0.35); print('done'); raise SystemExit(7)"
    command = shlex.join([sys.executable, "-c", source])
    manager = PtySessionManager()
    try:
        result = _tools(manager)["terminal_open"].executor({
            "command": command, "wait_for_exit": True, "idle": 0.01, "timeout": 5,
        })
        assert "start" in result
        assert "done" in result
        assert "exit_code=7" in result
    finally:
        manager.close_all()


@pytest.mark.skipif(os.name == "nt", reason="legacy terminal_open requires a Unix PTY")
def test_terminal_send_polls_without_writing_and_drains_completed_process():
    source = (
        "import select, sys; print('started'); "
        "ready, _, _ = select.select([sys.stdin], [], [], 1.5); "
        "print('unexpected input' if ready else 'finished')"
    )
    command = shlex.join([sys.executable, "-c", source])
    manager = PtySessionManager()
    try:
        tools = _tools(manager)
        first = tools["terminal_open"].executor({
            "command": command, "wait_for_exit": True, "timeout": 1,
        })
        assert "仍在运行" in first
        result = tools["terminal_send"].executor({"session_id": "term-1", "timeout": 5})
        assert "finished" in result
        assert "unexpected input" not in result
        assert "exit_code=0" in result
        assert "exit_code=0" in tools["terminal_send"].executor({"session_id": "term-1"})
    finally:
        manager.close_all()


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


@pytest.fixture(params=["pty", "pipe"])
def command_backend(request, monkeypatch):
    pipe = request.param == "pipe"
    if os.name == "nt" and not pipe:
        pytest.skip("PTY backend is unavailable on Windows")
    monkeypatch.setattr(I, "_IS_WINDOWS", pipe)
    return pipe


def test_exec_command_returns_exit_code_and_removes_completed_session(tmp_path, command_backend):
    from app.config.loader import use_workspace_root

    manager = PtySessionManager(str(tmp_path))
    try:
        with use_workspace_root(tmp_path):
            result = json.loads(_tools(manager)["exec_command"].executor({
                "command": "Write-Output 'hello'; exit 7" if os.name == "nt" else "printf 'hello\\n'; exit 7",
                "yield_time_ms": 5000,
            }))
        assert result == {"status": "exited", "exit_code": 7, "output": "hello"}
        assert manager.list() == []
    finally:
        manager.close_all()


@pytest.mark.parametrize(("tool_name", "running", "max_tokens", "text"), [
    ("exec_command", True, 3200, "中" * 3600),
    ("write_stdin", False, 3200, "中\\\t" * 1800),
    ("write_stdin", True, 128, "中" * 3600),
])
def test_command_session_budget_preserves_json_and_output_reference(
    tmp_path, monkeypatch, tool_name, running, max_tokens, text,
):
    import re
    from types import SimpleNamespace
    from app.agent.context_budget import estimate_tokens
    from app.agent.tool_batch import ToolCallBatch
    from app.config.loader import use_workspace_root
    from app.models.protocol import ToolCall
    from app.tools.output_budget import fits_output

    monkeypatch.setenv("AGENTLAB_OUTPUT_CACHE_DIR", str(tmp_path / "archive"))
    reads = iter([text, ""])
    session = SimpleNamespace(id="session-test", alive=running, exit_code=7,
                              read_until_idle=lambda **_: next(reads))
    manager = SimpleNamespace(open_argv=lambda *_, **__: (session.id, session),
                              get=lambda _: session, close=lambda _: None)
    args = {"max_output_tokens": max_tokens}
    args.update({"command": "sample"} if tool_name == "exec_command" else {"session_id": session.id})
    call = ToolCall("call", tool_name, args)

    with use_workspace_root(tmp_path):
        raw = _tools(manager)[tool_name].executor(args)
        assert fits_output(raw)
        assert ToolCallBatch({}, [call]).limit_output(call, raw, False) == raw
        result = json.loads(raw)
    assert result["status"] == ("running" if running else "exited")
    assert result["session_id" if running else "exit_code"] == (session.id if running else 7)
    body, reference = result["output"].rsplit("\n", 1)
    assert estimate_tokens(body) <= max_tokens
    assert re.search(r"out_[a-f0-9]{64}", reference)


def test_command_session_poll_and_stdin_share_approval_and_process(tmp_path, command_backend):
    from app.config.loader import use_workspace_root
    from app.tools.registry import ToolRegistry

    manager = PtySessionManager(str(tmp_path))
    registry = ToolRegistry()
    for tool in make_terminal_tools(manager):
        registry.register(tool)
    try:
        with use_workspace_root(tmp_path):
            command_tools = {"exec_command", "write_stdin"}
            assert not command_tools.intersection(t["name"] for t in registry.schemas_for_task("查看文件"))
            assert command_tools <= {t["name"] for t in registry.schemas_for_task("运行测试")}
            if os.name == "nt":
                command = "[Console]::Write('ready'); [Console]::Write([Console]::ReadLine())"
            else:
                command = "printf ready; read -r value; printf '%s' \"$value\""
                if not command_backend:
                    command = "stty -echo; " + command
            start = {"command": command, "yield_time_ms": 250}
            assert registry.execute("exec_command", start)[1]
            raw, error = registry.execute("exec_command", start, approved_action="exec_command")
            assert not error
            started = json.loads(raw)
            assert started["status"] == "running"
            assert command_tools <= {t["name"] for t in registry.schemas_for_task("继续")}
            sid = started["session_id"]
            poll = {"session_id": sid, "yield_time_ms": 1500}
            raw, error = registry.execute("write_stdin", poll)
            assert not error
            waiting = json.loads(raw)
            assert waiting["status"] == "running"
            assert started["output"] + waiting["output"] == "ready"
            write = {**poll, "chars": "finished\n", "yield_time_ms": 1000}
            assert registry.execute("write_stdin", write)[1]
            raw, error = registry.execute("write_stdin", write, approved_action="write_stdin")
            assert not error
            assert json.loads(raw) == {"status": "exited", "exit_code": 0, "output": "finished"}
            assert manager.get(sid) is None
    finally:
        manager.close_all()


def test_cancel_command_session_releases_manager_entry(tmp_path, command_backend):
    from app.agent.cancel import CancelToken, Cancelled
    from app.config.loader import use_workspace_root
    from app.tools.registry import ToolRegistry

    manager = PtySessionManager(str(tmp_path))
    registry = ToolRegistry()
    for tool in make_terminal_tools(manager):
        registry.register(tool)
    token = CancelToken()
    timer = threading.Timer(0.15, token.cancel)
    timer.start()
    try:
        with use_workspace_root(tmp_path), pytest.raises(Cancelled):
            command = "Start-Sleep -Seconds 30" if os.name == "nt" else "sleep 30"
            registry.execute("exec_command", {"command": command, "yield_time_ms": 10000},
                             approved_action="exec_command", cancel=token)
        assert manager.list() == []
    finally:
        timer.cancel()
        manager.close_all()


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
