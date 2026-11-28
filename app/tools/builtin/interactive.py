"""交互式终端会话 —— 通用的 PTY 持久会话能力。

为什么需要它:
  内置 `shell` 用 subprocess.run(capture_output=True),一次跑完就结束,没有
  stdin/PTY。这对"跑完即出结果"的命令够用,但搞不定 *交互式* 程序:
    - 远程登录后的 shell(ssh、vsm 隧道等):登录是一个会话,要逐条发命令
    - REPL(python、node、psql、redis-cli):需要持续对话
    - 交互式安装器 / 需要确认 y/N 的命令
    - 任何"先输出提示 → 等你输入 → 再输出"的程序
  这些程序常检测 TTY(没 TTY 就报 "inappropriate ioctl for device" 或行为变样),
  且必须跨多次工具调用保持同一个进程存活。

设计:
  - PtySession 包一个跑在伪终端里的子进程,提供 send()/read 当前可读输出。
  - 驱动方式是通用的 "read until idle":发完输入后持续读,直到输出静默一小段
    时间(说明程序在等下一次输入)或到硬超时。**不依赖任何提示符/哨兵约定**,
    所以对 vsm / ssh / python REPL 一视同仁。
  - PtySessionManager 按 id 管理多个会话,会话级生命周期(挂到 AgentSession
    的 closeables,会话结束时 close_all 杀掉所有子进程)。

安全:
  - 工具默认 requires_approval=True:开会话/发输入都可能在本机或远程执行任意
    命令,属高风险。
  - 不持有任何凭据:要连远程就 terminal_open 一个本机已配置好的命令(如
    `zsh -ic 'vsm <device>'`,凭据留在用户 shell 配置里),工具不碰账号密码。

Windows 兼容性:
  - pty 模块只在 Unix/Linux/macOS 上存在。Windows 的 exec_command 和
    write_stdin 使用 PowerShell 子进程及 stdin/stdout 管道。
"""
from __future__ import annotations

import errno
import json
import os
import platform
import re
import shlex
import signal
import threading
import time

from app.agent.context_budget import estimate_tokens
from app.tools.builtin.command_pipes import PipeCommandSession
from app.tools.output_budget import OUTPUT_TOKEN_LIMIT_SCHEMA, fit_prefix, fits_output, truncate_tool_output
from app.util.process_sandbox import ProcessSandboxError, sandbox_command

# Windows 的 select 不支持终端文件描述符。
_IS_WINDOWS = platform.system() == "Windows"
if not _IS_WINDOWS:
    import pty
    import select

# 单次返回给模型的输出上限,避免巨型日志撑爆上下文。
MAX_OUTPUT_BYTES = 4_000
# 读取时:输出静默多久就认为"程序在等输入了"(秒)。
DEFAULT_IDLE = 0.5
# open / send 的硬超时(秒):再久也返回,防止干等。
DEFAULT_OPEN_TIMEOUT = 45
DEFAULT_SEND_TIMEOUT = 30
DEFAULT_PROCESS_WAIT = 120
MAX_PROCESS_WAIT = 300

# 清 ANSI 转义码(颜色、光标移动);交互式程序的提示符常带大量颜色码。
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _normalize(raw: bytes) -> str:
    """PTY 输出解码 + 行尾归一(\\r\\n → \\n)+ 清 ANSI。"""
    text = raw.decode(errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return _strip_ansi(text)


def _clip(text: str, max_tokens: int | None = None, *, original: str | None = None) -> str:
    if max_tokens is not None:
        from app.tools.output_archive import archive_output, output_reference
        preview = truncate_tool_output(text, MAX_OUTPUT_BYTES, max_tokens=max_tokens)
        if preview == text and (original is None or original == text):
            return text
        output_id = archive_output(text if original is None else original)
        return preview + ("\n" + output_reference(output_id) if output_id else "")
    if len(text) <= MAX_OUTPUT_BYTES:
        return text
    from app.tools.output_archive import archive_output, output_reference
    path = archive_output(text)
    preview = text[:MAX_OUTPUT_BYTES // 2] + "\n…\n" + text[-MAX_OUTPUT_BYTES // 2:]
    return f"{preview}\n[...truncated, total {len(text)} characters]" + ("\n" + output_reference(path) if path else "")


def _command_result(status: dict, text: str, max_tokens: int | None) -> str:
    def serialize(body):
        return json.dumps({**status, "output": body}, ensure_ascii=False)

    def fits(body, suffix=""):
        return (len(body) <= MAX_OUTPUT_BYTES
                and (max_tokens is None or estimate_tokens(body) <= max_tokens)
                and fits_output(serialize(body + suffix)))

    if fits(text):
        return serialize(text)

    from app.tools.output_archive import archive_output, output_reference

    output_id = archive_output(text)
    suffix = "\n" + output_reference(output_id) if output_id else ""
    notice = "\n[命令输出已截断]"
    prefix = fit_prefix(text[:MAX_OUTPUT_BYTES], lambda body: fits(body + notice, suffix))
    return serialize(prefix + notice + suffix)


class PtySession:
    """跑在伪终端里的一个子进程会话。Unix/Linux/macOS only."""

    def __init__(self, session_id: str, argv: list[str], cwd: str | None = None):
        if _IS_WINDOWS:
            raise NotImplementedError("PTY sessions are not supported on Windows")
        self.id = session_id
        self.argv = argv
        self._pid, self._fd = pty.fork()
        if self._pid == 0:
            # 子进程:成为会话首进程,exec 目标命令。
            try:
                if cwd:
                    os.chdir(cwd)
                os.execvp(argv[0], argv)
            except OSError:
                os._exit(127)
        self._closed = False
        self._eof = False
        self.exit_code: int | None = None
        self.wait_for_exit = False

    # ── 读 / 写 ──────────────────────────────────────────────────────────────

    def read_until_idle(
        self,
        idle: float | None = DEFAULT_IDLE,
        timeout: float = DEFAULT_SEND_TIMEOUT,
        cancel=None,
    ) -> str:
        """读取到静默或硬超时。idle=None 时等进程退出或硬超时。"""
        chunks: list[str] = []
        deadline = time.monotonic() + timeout
        last_data = time.monotonic()
        while (remaining := deadline - time.monotonic()) > 0:
            if cancel is not None:
                cancel.raise_if_cancelled()
            interval = min(0.1, remaining)
            if self._eof:
                if idle is not None or not self.alive:
                    break
                time.sleep(interval)
                continue
            r, _, _ = select.select([self._fd], [], [], interval)
            if self._fd in r:
                try:
                    data = os.read(self._fd, 4096)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    self._eof = True
                    continue
                chunks.append(_normalize(data))
                last_data = time.monotonic()
            else:
                if idle is None and not self.alive:
                    break
                # 没有新数据。已经读到过东西且静默够久 → 认为在等输入,返回。
                if idle is not None and chunks and time.monotonic() - last_data >= idle:
                    break
        return "".join(chunks)

    def send(self, data: str, append_newline: bool = True, *, cancel=None) -> None:
        """向会话写入一行输入(默认补换行,相当于"按回车")。"""
        if self._closed or self._eof:
            raise OSError("session closed")
        payload = (data + ("\n" if append_newline and not data.endswith("\n") else "")).encode()
        deadline = time.monotonic() + 1
        sent = 0
        os.set_blocking(self._fd, False)
        try:
            while sent < len(payload):
                if cancel is not None:
                    cancel.raise_if_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OSError(f"stdin write timed out after {sent} of {len(payload)} bytes")
                _, writable, _ = select.select([], [self._fd], [], min(0.1, remaining))
                if writable:
                    try:
                        sent += os.write(self._fd, payload[sent:])
                    except BlockingIOError:
                        continue
        finally:
            os.set_blocking(self._fd, True)

    # ── 状态 / 清理 ────────────────────────────────────────────────────────────

    @property
    def accepts_input(self) -> bool:
        return not self._eof and self.alive

    @property
    def alive(self) -> bool:
        """子进程是否还活着(非阻塞探测)。"""
        if self._closed:
            return False
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
            if pid == self._pid:
                self.exit_code = os.waitstatus_to_exitcode(status)
                self._closed = True
                return False
        except OSError:
            self._closed = True
            return False
        return True

    def close(self) -> None:
        """关闭会话:先发 SIGTERM(Unix)或 SIGINT(Win),再关 fd,回收子进程。重复调用安全。"""
        if self._closed and self._fd < 0:
            return
        try:
            # Windows 上没有 SIGTERM，用 SIGINT 代替
            sig = signal.SIGTERM if hasattr(signal, 'SIGTERM') else signal.SIGINT
            if self.alive:
                try:
                    os.killpg(self._pid, sig)
                except ProcessLookupError:
                    # A newly forked child may still be setting up its group.
                    os.kill(self._pid, sig)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = -1
        # 回收僵尸进程(给一点时间退出,然后 WNOHANG 收割)
        for _ in range(5):
            try:
                pid, _ = os.waitpid(self._pid, os.WNOHANG)
                if pid == self._pid:
                    break
            except OSError as exc:
                if exc.errno == errno.ECHILD:
                    break
            time.sleep(0.05)
        else:
            try:
                os.killpg(self._pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

            def reap():
                try:
                    os.waitpid(self._pid, 0)
                except ChildProcessError:
                    pass

            threading.Thread(target=reap, daemon=True, name="agentlab-pty-reap").start()
        self._closed = True


class PtySessionManager:
    """管理会话内 PTY 或管道子进程的生命周期。"""

    def __init__(self, cwd: str | None = None):
        self._sessions: dict[str, PtySession | PipeCommandSession] = {}
        self._counter = 0
        self._cwd = cwd

    def open(self, command: str) -> tuple[str, PtySession]:
        """启动一个新会话,返回 (session_id, session)。command 用 shlex 解析。"""
        return self.open_argv(shlex.split(command))

    def open_argv(
        self, argv: list[str], *, cwd: str | None = None,
    ) -> tuple[str, PtySession | PipeCommandSession]:
        if not argv:
            raise ValueError("empty command")
        self._counter += 1
        sid = f"term-{self._counter}"
        backend = PipeCommandSession if _IS_WINDOWS else PtySession
        workdir = cwd or self._cwd
        session = backend(sid, sandbox_command(argv, cwd=workdir), cwd=workdir)
        self._sessions[sid] = session
        return sid, session

    def get(self, session_id: str) -> PtySession | PipeCommandSession | None:
        return self._sessions.get(session_id)

    def list(self) -> list[PtySession | PipeCommandSession]:
        return list(self._sessions.values())

    def close(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        session.close()
        return True

    def close_all(self) -> None:
        """会话结束时调用,杀掉所有子进程。AgentSession.close 会触发。"""
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()

    # AgentSession.close 会找 .stop / .close 方法,这里提供 stop 别名。
    def stop(self) -> None:
        self.close_all()


# ── 工具工厂 ──────────────────────────────────────────────────────────────────
# terminal_* 工具有状态(共享同一个 manager),所以用工厂按会话注入,
# 跟 make_todo_write_tool(task_store) 一个套路。

def make_terminal_tools(manager: PtySessionManager) -> list:
    """为同一会话注册命令执行、等待和兼容终端工具。"""
    from app.agent.cancel import Cancelled
    from app.tools.builtin.files import WorkspacePathError, _outside_workspace_approval, _resolve_within_workspace
    from app.tools.builtin.shell import _build_argv
    from app.tools.registry import Tool, ToolExecutionError, current_cancel_token

    def collect(session: PtySession | PipeCommandSession, wait_ms: int, max_tokens: int | None) -> str:
        cancel = current_cancel_token()
        try:
            output = session.read_until_idle(idle=None, timeout=wait_ms / 1000, cancel=cancel)
            if cancel is not None:
                cancel.raise_if_cancelled()
            if session.alive:
                result = {"status": "running", "session_id": session.id}
            else:
                # The process may exit just after yielding with unread output.
                output += session.read_until_idle(idle=None, timeout=0.1, cancel=cancel)
                result = {"status": "exited", "exit_code": session.exit_code}
                manager.close(session.id)
        except (Cancelled, OSError) as exc:
            manager.close(session.id)
            if isinstance(exc, Cancelled):
                raise
            raise ToolExecutionError(f"command session failed: {exc}") from exc
        return _command_result(result, output.strip(), max_tokens)

    def exec_command(args: dict) -> str:
        command = args["command"].strip()
        if not command:
            raise ToolExecutionError("empty command")
        try:
            cwd = _resolve_within_workspace(args.get("cwd", "."), "exec_command_outside_workspace")
        except WorkspacePathError as exc:
            raise ToolExecutionError(str(exc)) from exc
        if not cwd.is_dir():
            raise ToolExecutionError(f"working directory not found: {cwd}")
        if (cancel := current_cancel_token()) is not None:
            cancel.raise_if_cancelled()
        try:
            _, session = manager.open_argv(_build_argv(command), cwd=str(cwd))
        except (OSError, ProcessSandboxError) as exc:
            raise ToolExecutionError(f"failed to start command: {exc}") from exc
        return collect(session, args.get("yield_time_ms", 1000), args.get("max_output_tokens"))

    def write_stdin(args: dict) -> str:
        sid = args["session_id"]
        session = manager.get(sid)
        if session is None:
            raise ToolExecutionError(f"unknown session_id: {sid}")
        chars = args.get("chars", "")
        if chars:
            try:
                session.send(chars, append_newline=False, cancel=current_cancel_token())
            except Cancelled:
                manager.close(sid)
                raise
            except OSError as exc:
                manager.close(sid)
                raise ToolExecutionError(f"failed to write stdin: {exc}") from exc
        default_wait = 250 if chars else 10000
        return collect(session, min(args.get("yield_time_ms", default_wait), 30000 if chars else 300000),
                       args.get("max_output_tokens"))

    def process_status(session: PtySession | PipeCommandSession) -> str:
        if session.alive:
            return "\n[进程仍在运行；可用 terminal_send 空输入继续等待]"
        if session.exit_code is None:
            return "\n[进程已结束]"
        return f"\n[进程已结束，exit_code={session.exit_code}]"

    def process_wait(args: dict, default: int) -> int:
        try:
            return max(1, min(MAX_PROCESS_WAIT, int(args.get("timeout") or default)))
        except (TypeError, ValueError, OverflowError):
            return default

    def _open(args: dict) -> str:
        if _IS_WINDOWS:
            return (
                "交互式终端会话在 Windows 上不支持(需要 Unix PTY)。\n"
                "Windows 上可用 exec_command 和 write_stdin 运行通过管道交互的命令。"
            )
        command = (args.get("command") or "").strip()
        if not command:
            return "refused: empty command"
        try:
            idle = float(args.get("idle") or DEFAULT_IDLE)
        except (TypeError, ValueError):
            idle = DEFAULT_IDLE
        try:
            timeout = int(args.get("timeout") or DEFAULT_OPEN_TIMEOUT)
        except (TypeError, ValueError):
            timeout = DEFAULT_OPEN_TIMEOUT
        try:
            sid, session = manager.open(command)
        except ProcessSandboxError as exc:
            raise ToolExecutionError(str(exc)) from exc
        except ValueError as exc:
            return f"refused: {exc}"
        except OSError as exc:
            return f"failed to start session: {exc}"
        session.wait_for_exit = bool(args.get("wait_for_exit", False))
        if session.wait_for_exit:
            idle = None
            timeout = process_wait(args, DEFAULT_PROCESS_WAIT)
        initial = session.read_until_idle(
            idle=idle, timeout=timeout, cancel=current_cancel_token(),
        )
        status = process_status(session) if session.wait_for_exit else (
            "" if session.accepts_input else "\n[session 已结束]"
        )
        from app.tools.rtk_builtin import BuiltinRTK
        body = BuiltinRTK().compress_terminal(command, initial.strip()) or "(无输出)"
        return f"[session {sid}]\n{_clip(body, args.get('max_output_tokens'), original=initial.strip())}{status}"

    def _send(args: dict) -> str:
        sid = (args.get("session_id") or "").strip()
        if not sid:
            return "refused: 缺少 session_id"
        session = manager.get(sid)
        if session is None:
            return f"refused: 未知 session_id '{sid}'(用 terminal_list 查看活跃会话)"
        data = args.get("input", "" if session.wait_for_exit else None)
        if data is None:
            return "refused: 缺少 input"
        append_nl = bool(args.get("enter", not session.wait_for_exit or bool(data)))
        polling = session.wait_for_exit and data == "" and not append_nl
        if not polling and not session.accepts_input:
            return f"refused: session {sid} 已结束(用 terminal_open 重开)"
        try:
            idle = float(args.get("idle") or DEFAULT_IDLE)
        except (TypeError, ValueError):
            idle = DEFAULT_IDLE
        try:
            timeout = int(args.get("timeout") or DEFAULT_SEND_TIMEOUT)
        except (TypeError, ValueError):
            timeout = DEFAULT_SEND_TIMEOUT
        if polling:
            idle = None
            timeout = process_wait(args, MAX_PROCESS_WAIT)
        try:
            if not polling:
                session.send(str(data), append_newline=append_nl)
        except OSError as exc:
            return f"failed to send: {exc}"
        out = session.read_until_idle(
            idle=idle, timeout=timeout, cancel=current_cancel_token(),
        )
        status = process_status(session) if session.wait_for_exit else (
            "" if session.accepts_input else f"\n[session {sid} 已结束]"
        )
        from app.tools.rtk_builtin import BuiltinRTK
        body = BuiltinRTK().compress_terminal(str(data), out.strip()) or "(无输出)"
        return f"{_clip(body, args.get('max_output_tokens'), original=out.strip())}{status}"

    def _close(args: dict) -> str:
        sid = (args.get("session_id") or "").strip()
        if not sid:
            return "refused: 缺少 session_id"
        ok = manager.close(sid)
        return f"已关闭 session {sid}" if ok else f"未知 session_id '{sid}'"

    def _list(args: dict) -> str:
        sessions = manager.list()
        if not sessions:
            return "无活跃 terminal 会话。"
        lines = ["session_id    状态      命令"]
        for s in sessions:
            state = "alive" if s.alive else "dead"
            lines.append(f"  {s.id:<12}{state:<8}{' '.join(s.argv)}")
        return "\n".join(lines)

    open_tool = Tool(
        name="terminal_open",
        description=(
            "启动一个交互式终端会话(伪终端 PTY),用于需要持续交互的程序:"
            "远程登录(如 zsh -ic 'vsm <device>'、ssh)、REPL(python、psql)、"
            "交互式安装器等。返回 session_id 和启动后的首屏输出。"
            "随后用 terminal_send 向该会话发命令、terminal_close 关闭。"
            "安装、下载、构建等能自行结束的命令使用 wait_for_exit=true，"
            "应给足 timeout，让工具等待退出码，避免按输出进度重复查询。"
            "区别于 shell:shell 跑完即结束、无交互;这个保持进程存活、可多轮对话。"
            "不要在 command 里写账号密码,远程凭据应由本机命令配置自带。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "max_output_tokens": OUTPUT_TOKEN_LIMIT_SCHEMA,
                "command": {
                    "type": "string",
                    "description": "启动会话的命令,例如 \"zsh -ic 'vsm orangepi-xxx'\" 或 'python3'",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"等待上限（秒）。交互模式默认 {DEFAULT_OPEN_TIMEOUT}；"
                    f"wait_for_exit 模式默认 {DEFAULT_PROCESS_WAIT}，最多 {MAX_PROCESS_WAIT}。",
                },
                "wait_for_exit": {
                    "type": "boolean",
                    "description": "适合非交互长命令。等退出或超时，即使输出静默也会继续等待。",
                    "default": False,
                },
                "idle": {
                    "type": "number",
                    "description": f"交互模式输出静默多久返回（秒），默认 {DEFAULT_IDLE}。",
                    "default": DEFAULT_IDLE,
                },
            },
            "required": ["command"],
        },
        executor=_open,
        risk="execute",
        target_type="terminal_session",
        scope="session",
        origin="builtin",
        requires_observation=True,
        requires_approval=True,
    )

    send_tool = Tool(
        name="terminal_send",
        description=(
            "向一个已打开的交互式会话发送输入(默认末尾补回车),返回这次输入后"
            "新产生的输出。用于在 terminal_open 起的会话里逐条执行命令/回答提示。"
            "对 wait_for_exit 会话，省略 input 或传空字符串且 enter=false 可等待进程结束。"
            "例:在 vsm 远程会话里发 'df -h';在 python REPL 里发 'print(1+1)'。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "max_output_tokens": OUTPUT_TOKEN_LIMIT_SCHEMA,
                "session_id": {
                    "type": "string",
                    "description": "terminal_open 返回的会话 id,例如 'term-1'",
                },
                "input": {
                    "type": "string",
                    "description": "要发送的内容。wait_for_exit 会话可省略以等待退出。",
                },
                "enter": {
                    "type": "boolean",
                    "description": "是否补回车。默认 true；wait_for_exit 会话的空输入默认 false。",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"读输出硬超时（秒）。交互默认 {DEFAULT_SEND_TIMEOUT}；"
                    f"等待退出的轮询默认 {MAX_PROCESS_WAIT}，也是上限。",
                },
                "idle": {
                    "type": "number",
                    "description": f"输出静默多少秒视为本次结束,默认 {DEFAULT_IDLE}",
                    "default": DEFAULT_IDLE,
                },
            },
            "required": ["session_id"],
        },
        executor=_send,
        risk="execute",
        target_type="terminal_session",
        scope="session",
        origin="builtin",
        requires_observation=True,
        requires_approval=True,
    )

    close_tool = Tool(
        name="terminal_close",
        description="关闭一个交互式会话,杀掉其子进程。用完会话(如远程登录、REPL)后应关闭。",
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "要关闭的会话 id",
                },
            },
            "required": ["session_id"],
        },
        executor=_close,
        risk="execute",
        target_type="terminal_session",
        scope="session",
        origin="builtin",
        requires_approval=False,  # 关闭是收尾动作,无需审批
    )

    list_tool = Tool(
        name="terminal_list",
        description="列出当前所有活跃的交互式会话(session_id、存活状态、启动命令)。",
        input_schema={"type": "object", "properties": {}},
        executor=_list,
        risk="observe",
        target_type="terminal_session",
        scope="session",
        origin="builtin",
        requires_approval=False,
    )

    wait_schema = {"type": "integer", "minimum": 1, "maximum": 300000}
    return [
        open_tool, send_tool, close_tool, list_tool,
        Tool(
            name="exec_command",
            description=("执行命令并返回退出码或活跃 session_id。"
                         "Windows 使用 PowerShell，启用系统沙箱时使用 WSL2 Bash。"
                         "短命令直接获取输出；长任务用 write_stdin 空输入继续等待。"
                         "输出截断后可用 read_tool_output 读取完整记录。"),
            input_schema={"type": "object", "properties": {
                "max_output_tokens": OUTPUT_TOKEN_LIMIT_SCHEMA,
                "command": {"type": "string", "minLength": 1},
                "cwd": {"type": "string", "description": "工作目录，默认当前工作区。"},
                "yield_time_ms": {**wait_schema, "description": "返回前等待的毫秒数，默认 1000。"},
            }, "required": ["command"], "additionalProperties": False},
            executor=exec_command, risk="execute", target_type="process", origin="builtin",
            scope="workspace_or_approved_external", requires_approval=True,
            approval_resolver=lambda args: _outside_workspace_approval("exec_command", args, path_key="cwd"),
        ),
        Tool(
            name="write_stdin",
            description=("读取命令会话的新输出或发送字符。空 chars 只等待，默认 10 秒；"
                         "发送时按原样写入，需回车请显式提供换行。可用 terminal_close 停止会话。"),
            input_schema={"type": "object", "properties": {
                "max_output_tokens": OUTPUT_TOKEN_LIMIT_SCHEMA,
                "session_id": {"type": "string", "minLength": 1},
                "chars": {"type": "string", "maxLength": 4096},
                "yield_time_ms": wait_schema,
            }, "required": ["session_id"], "additionalProperties": False},
            executor=write_stdin, risk="execute", target_type="terminal_session", scope="session",
            origin="builtin", requires_approval=False,
            approval_resolver=lambda args: "write_stdin" if args.get("chars") else None,
        ),
    ]
