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
MAX_OUTPUT_BYTES = 8_000
# 读取时:输出静默多久就认为"程序在等输入了"(秒)。
DEFAULT_IDLE = 0.5
# open / send 的硬超时(秒):再久也返回,防止干等。
DEFAULT_OPEN_TIMEOUT = 45
DEFAULT_SEND_TIMEOUT = 30

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
    return f"{text[:MAX_OUTPUT_BYTES]}\n\n[...truncated, total {len(text)} bytes]"


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

    # ── 读 / 写 ──────────────────────────────────────────────────────────────

    def read_until_idle(self, idle: float = DEFAULT_IDLE,
                        timeout: float = DEFAULT_SEND_TIMEOUT) -> str:
        """持续读输出,直到静默 `idle` 秒(程序在等输入)或到 `timeout` 硬超时。

        这是通用驱动的核心:不假设任何提示符,只靠"输出停了"来判断该把控制权
        交回给模型。慢启动的程序(ssh 建连、隧道)靠 timeout 兜住首字节延迟。
        """
        chunks: list[str] = []
        deadline = time.time() + timeout
        last_data = time.time()
        while time.time() < deadline:
            r, _, _ = select.select([self._fd], [], [], 0.1)
            if self._fd in r:
                try:
                    data = os.read(self._fd, 4096)
                except OSError:
                    self._closed = True
                    break
                if not data:  # EOF:子进程退出
                    self._closed = True
                    break
                chunks.append(_normalize(data))
                last_data = time.time()
            else:
                # 没有新数据。已经读到过东西且静默够久 → 认为在等输入,返回。
                if chunks and (time.time() - last_data) >= idle:
                    break
        return "".join(chunks)

    def send(self, data: str, append_newline: bool = True) -> None:
        """向会话写入一行输入(默认补换行,相当于"按回车")。"""
        if self._closed:
            raise OSError("session closed")
        payload = data + ("\n" if append_newline and not data.endswith("\n") else "")
        os.write(self._fd, payload.encode())

    # ── 状态 / 清理 ────────────────────────────────────────────────────────────

    @property
    def alive(self) -> bool:
        """子进程是否还活着(非阻塞探测)。"""
        if self._closed:
            return False
        try:
            pid, _ = os.waitpid(self._pid, os.WNOHANG)
            if pid == self._pid:
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
    from app.tools.registry import Tool

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
        # 读启动后的首屏输出(登录横幅、提示符、REPL 欢迎语等)
        initial = session.read_until_idle(idle=idle, timeout=timeout)
        status = "" if session.alive else "\n[session 已结束]"
        body = initial.strip() or "(无输出)"
        return f"[session {sid}]\n{_clip(body)}{status}"

    def _send(args: dict) -> str:
        sid = (args.get("session_id") or "").strip()
        if not sid:
            return "refused: 缺少 session_id"
        session = manager.get(sid)
        if session is None:
            return f"refused: 未知 session_id '{sid}'(用 terminal_list 查看活跃会话)"
        if not session.alive:
            return f"refused: session {sid} 已结束(用 terminal_open 重开)"
        data = args.get("input")
        if data is None:
            return "refused: 缺少 input"
        try:
            idle = float(args.get("idle") or DEFAULT_IDLE)
        except (TypeError, ValueError):
            idle = DEFAULT_IDLE
        try:
            timeout = int(args.get("timeout") or DEFAULT_SEND_TIMEOUT)
        except (TypeError, ValueError):
            timeout = DEFAULT_SEND_TIMEOUT
        append_nl = bool(args.get("enter", True))
        try:
            session.send(str(data), append_newline=append_nl)
        except OSError as exc:
            return f"failed to send: {exc}"
        out = session.read_until_idle(idle=idle, timeout=timeout)
        status = "" if session.alive else f"\n[session {sid} 已结束]"
        body = out.strip() or "(无输出)"
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
                    "description": f"等待首屏输出的硬超时秒数,默认 {DEFAULT_OPEN_TIMEOUT}(远程建连较慢)",
                    "default": DEFAULT_OPEN_TIMEOUT,
                },
                "idle": {
                    "type": "number",
                    "description": f"输出静默多少秒视为就绪,默认 {DEFAULT_IDLE}",
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
                    "description": "要发送的内容,例如 'ls -la' 或回答提示的 'y'",
                },
                "enter": {
                    "type": "boolean",
                    "description": "是否在末尾补回车,默认 true。发单个控制字符时可设 false",
                    "default": True,
                },
                "timeout": {
                    "type": "integer",
                    "description": f"读输出硬超时秒数,默认 {DEFAULT_SEND_TIMEOUT}",
                    "default": DEFAULT_SEND_TIMEOUT,
                },
                "idle": {
                    "type": "number",
                    "description": f"输出静默多少秒视为本次结束,默认 {DEFAULT_IDLE}",
                    "default": DEFAULT_IDLE,
                },
            },
            "required": ["session_id", "input"],
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
