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
  - pty 模块只在 Unix/Linux/macOS 上存在。Windows 上会优雅降级:工具仍注册,
    但调用时返回 "不支持" 错误而不是导入失败崩溃整个 CLI。
"""
from __future__ import annotations

import errno
import os
import platform
import re
import shlex
import signal
import time

# pty 和 select 只在 Unix 系统上存在
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


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_BYTES:
        return text
    from app.tools.output_archive import archive_output, output_reference
    path = archive_output(text)
    preview = text[:MAX_OUTPUT_BYTES // 2] + "\n…\n" + text[-MAX_OUTPUT_BYTES // 2:]
    return f"{preview}\n[...truncated, total {len(text)} characters]" + ("\n" + output_reference(path) if path else "")


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

    def send(self, data: str, append_newline: bool = True) -> None:
        """向会话写入一行输入(默认补换行,相当于"按回车")。"""
        if self._closed or self._eof:
            raise OSError("session closed")
        payload = data + ("\n" if append_newline and not data.endswith("\n") else "")
        os.write(self._fd, payload.encode())

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
        self._closed = True


class PtySessionManager:
    """管理一组 PtySession 的生命周期。会话级单例,挂到 AgentSession.closeables。"""

    def __init__(self, cwd: str | None = None):
        self._sessions: dict[str, PtySession] = {}
        self._counter = 0
        self._cwd = cwd

    def open(self, command: str) -> tuple[str, PtySession]:
        """启动一个新会话,返回 (session_id, session)。command 用 shlex 解析。"""
        argv = shlex.split(command)
        if not argv:
            raise ValueError("empty command")
        self._counter += 1
        sid = f"term-{self._counter}"
        session = PtySession(sid, argv, cwd=self._cwd)
        self._sessions[sid] = session
        return sid, session

    def get(self, session_id: str) -> PtySession | None:
        return self._sessions.get(session_id)

    def list(self) -> list[PtySession]:
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
    """返回绑定到指定 manager 的 terminal_open / send / close / list 四个工具。"""
    from app.tools.registry import Tool, current_cancel_token

    def process_status(session: PtySession) -> str:
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
                "Windows 用户请直接在命令行/PowerShell 执行交互式命令,或考虑 WSL。"
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
        return f"[session {sid}]\n{_clip(body)}{status}"

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
        return f"{_clip(body)}{status}"

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

    return [open_tool, send_tool, close_tool, list_tool]
