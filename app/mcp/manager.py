"""MCP Manager —— 连接 MCP Server、发现工具、把异步调用桥接成同步。

核心难点(sync ↔ async 桥):
  Runtime 的工具执行是同步的(executor(args) -> str),但 MCP SDK 是 asyncio 的,
  而且浏览器这类 session 必须"跨多次工具调用存活"(状态在 server 进程/浏览器里)。

方案:
  - 在后台线程跑一个常驻 event loop。
  - 每个 server 用一个长生命周期的 _serve() 任务:进入 stdio + ClientSession 两个
    async 上下文 → initialize → list_tools → 把 session 交给主线程 → 阻塞等 shutdown。
    关键:async 上下文(anyio)要求 __aenter__/__aexit__ 在同一个 task 里,所以进入和
    退出都在 _serve 这一个 task 内完成,绝不能跨 task 退出。
  - 工具调用走 asyncio.run_coroutine_threadsafe(session.call_tool(...)) 投递进 loop,
    同步阻塞等结果。call_tool 只是往 anyio 流里写消息再等回包,跨 task 调用是安全的
    (cancel scope 限制只针对上下文的进入/退出,不针对方法调用)。

安全:
  - 子进程 env 只包含跨平台运行所需的非敏感系统变量,以及
    server.env_allowlist 显式允许的变量,避免把密钥泄漏给 MCP server。
  - 工具输出经 redact() 脱敏后才回灌给(可能是云端的)模型。
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
import sys
import threading
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.mcp.config import MCPServerConfig, PROJECT_ROOT
from app.util.redact import format_exception, redact


_COMMON_RUNTIME_ENV_VARS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
    "USERNAME",
)

# CreateProcess、Node.js/npm 和 Playwright 在 Windows 上依赖这些路径变量。
# 它们只包含本机路径/系统配置,不包含 API key 或业务凭据。
_WINDOWS_RUNTIME_ENV_VARS = (
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PATHEXT",
    "PROGRAMDATA",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)

_WINDOWS_EXECUTABLE_EXTENSIONS = (".cmd", ".exe", ".bat", ".com")


def _format_connect_error(exc: BaseException) -> str:
    """展开 ExceptionGroup，避免 MCP 启动只显示无信息的外层错误。"""
    nested = getattr(exc, "exceptions", None)
    if nested:
        details = "; ".join(_format_connect_error(item) for item in nested)
        return f"{type(exc).__name__}: {details}"
    cause = exc.__cause__ or exc.__context__
    current = format_exception(exc)
    if cause is not None and cause is not exc:
        return f"{current} <- {_format_connect_error(cause)}"
    return current


def _resolve_stdio_command(command: str | None, system: str | None = None) -> str:
    """把 MCP stdio 命令解析为可直接启动的可执行文件。

    Windows 的 npm shim 通常是 ``npx.cmd``。MCP stdio 禁止使用 shell=True,
    所以必须在启动前显式解析扩展名;同一份 ``command: npx`` 配置因此可在三个
    平台复用。
    """
    raw = (command or "").strip()
    if not raw:
        raise ValueError("MCP stdio server 缺少 command")

    expanded = os.path.expandvars(os.path.expanduser(raw))
    candidates = [expanded]
    if (system or platform.system()) == "Windows":
        lower = expanded.lower()
        if not lower.endswith(_WINDOWS_EXECUTABLE_EXTENSIONS):
            # Node/npm 在 Windows 同时安装无扩展名的 Unix shell shim 和 .cmd。
            # shutil.which("npx") 可能先返回不可由 CreateProcess 直接启动的 npx，
            # 导致 WinError 5/193；必须优先解析 Windows 可执行扩展名。
            candidates = [
                *(f"{expanded}{ext}" for ext in _WINDOWS_EXECUTABLE_EXTENSIONS),
                expanded,
            ]

    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved

    hint = ""
    if raw.lower() in {"npx", "npx.cmd"}:
        hint = " 请安装 Node.js LTS，并重新打开终端以刷新 PATH。"
    raise FileNotFoundError(f"找不到 MCP 启动命令 '{raw}'。{hint}".rstrip())


def _resolve_stdio_launch(
    command: str | None,
    args: list[str],
    system: str | None = None,
) -> tuple[str, list[str]]:
    """把 Windows npm/npx shim 展开为 node.exe + CLI JS，避免 WinError 5。"""
    resolved = _resolve_stdio_command(command, system=system)
    if (system or platform.system()) != "Windows":
        return resolved, list(args)
    path = Path(resolved)
    shim = path.name.lower()
    cli_names = {
        "npx.cmd": "npx-cli.js",
        "npm.cmd": "npm-cli.js",
    }
    cli_name = cli_names.get(shim)
    if cli_name is None:
        return resolved, list(args)
    node = path.parent / "node.exe"
    cli = path.parent / "node_modules" / "npm" / "bin" / cli_name
    if not node.is_file() or not cli.is_file():
        raise FileNotFoundError(
            f"无法展开 Windows {shim}: 缺少 {node if not node.is_file() else cli}"
        )
    return str(node), [str(cli), *args]


def _windows_default_browser_progid() -> str:
    """读取当前用户 HTTPS 关联的默认浏览器 ProgId。"""
    import winreg

    key_path = (
        r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations"
        r"\https\UserChoice"
    )
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        value, _ = winreg.QueryValueEx(key, "ProgId")
    return str(value or "")


def _playwright_channel_for_progid(progid: str) -> str:
    """把 Windows 默认浏览器 ProgId 映射为 Playwright MCP channel。"""
    normalized = (progid or "").lower()
    if "edge" in normalized:
        return "msedge"
    if "chrome" in normalized or "chromium" in normalized:
        return "chrome"
    if "firefox" in normalized:
        return "firefox"
    raise RuntimeError(
        f"默认浏览器 ProgId '{progid or 'unknown'}' 暂不能映射到 Playwright；"
        "请在 MCP 配置中显式使用 chrome/firefox/webkit/msedge"
    )


def _resolve_playwright_browser_args(
    args: list[str],
    system: str | None = None,
) -> list[str]:
    """把配置中的 system-default 动态解析为当前系统默认浏览器 channel。"""
    resolved = list(args)
    for index, value in enumerate(resolved[:-1]):
        if value != "--browser" or resolved[index + 1] not in {"default", "system-default"}:
            continue
        if (system or platform.system()) != "Windows":
            raise RuntimeError("--browser system-default 当前仅支持 Windows")
        resolved[index + 1] = _playwright_channel_for_progid(
            _windows_default_browser_progid()
        )
    return resolved


def _build_stdio_env(
    server: MCPServerConfig,
    system: str | None = None,
) -> dict[str, str]:
    """构造最小、可跨平台运行的 MCP 子进程环境。"""
    keys = list(_COMMON_RUNTIME_ENV_VARS)
    if (system or platform.system()) == "Windows":
        keys.extend(_WINDOWS_RUNTIME_ENV_VARS)
    keys.extend(server.env_allowlist)

    env: dict[str, str] = {}
    for key in dict.fromkeys(keys):
        value = os.environ.get(key)
        if value is None or value.startswith("()"):
            continue
        env[key] = value
    return env


def _resolve_stdio_cwd(cwd: str | None) -> str | None:
    """将配置中的相对 cwd 固定到项目根目录,避免受启动终端目录影响。"""
    if not cwd:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(cwd)))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


@dataclass
class MCPToolInfo:
    """从 MCP server 发现的一个工具(已规整为内部表示)。"""
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]


def _mcp_tool_input_schema(tool: Any) -> dict[str, Any]:
    """兼容 MCP SDK 1.x inputSchema 与 2.x input_schema。"""
    return dict(
        getattr(tool, "input_schema", None)
        or getattr(tool, "inputSchema", None)
        or {}
    )


def _content_to_text(result: Any) -> tuple[str, bool]:
    """把 MCP CallToolResult 转成 (文本, 是否错误)。

    content 是一组 block:text 直接取 .text;image/audio 只回占位说明(MVP 不回传
    二进制给模型);其它类型回 repr 兜底。输出整体过 redact()。
    is_error 取 result.isError(SDK 字段),取不到则按无错误处理。
    """
    # MCP Python SDK 的 Pydantic 模型使用 snake_case 属性 is_error，旧版或
    # 其它实现可能仍暴露 JSON 字段名 isError。两者都要兼容，否则服务端已经
    # 标记失败的结果会在 CLI 中错误显示为 [ok]。
    raw_is_error = getattr(result, "is_error", None)
    if raw_is_error is None:
        raw_is_error = getattr(result, "isError", False)
    is_error = bool(raw_is_error)
    blocks = getattr(result, "content", None) or []
    parts: list[str] = []
    for block in blocks:
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(getattr(block, "text", ""))
        elif btype == "image":
            mime = getattr(block, "mimeType", "image")
            data = getattr(block, "data", "") or ""
            parts.append(f"[image omitted: {mime}, ~{len(data)} b64 chars]")
        elif btype == "audio":
            parts.append("[audio omitted]")
        elif btype == "resource":
            res = getattr(block, "resource", None)
            uri = getattr(res, "uri", "") if res else ""
            parts.append(f"[resource: {uri}]")
        else:
            parts.append(str(block))
    text = "\n".join(p for p in parts if p) or "(empty result)"
    # 少数 MCP server 把错误仅写进文本而没有设置协议错误位。
    if text.lstrip().lower().startswith(("### error", "error:", "[error]")):
        is_error = True
    return redact(text), is_error


class MCPManager:
    """管理所有 enabled MCP server 的连接生命周期与工具调用。"""

    def __init__(self, servers: list[MCPServerConfig], connect_timeout: float = 60.0):
        self.servers = servers
        self.connect_timeout = connect_timeout
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self._sessions: dict[str, Any] = {}          # server name -> ClientSession
        self._tools: dict[str, list[MCPToolInfo]] = {}  # server name -> tools
        self._shutdowns: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # ── loop 线程 ────────────────────────────────────────────────────────────
    def _ensure_loop(self) -> None:
        if self.loop is not None:
            return
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="mcp-loop")
        self.thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    # ── 连接 ─────────────────────────────────────────────────────────────────
    def start(self) -> None:
        """启动 loop 线程,连接所有 server。单个 server 连接失败只警告,不影响其它。"""
        if not self.servers:
            return
        self._ensure_loop()
        for server in self.servers:
            if server.transport != "stdio":
                print(f"⚠ MCP server '{server.name}': 暂只支持 stdio transport,跳过",
                      file=sys.stderr)
                continue
            try:
                fut = asyncio.run_coroutine_threadsafe(self._connect(server), self.loop)
                session, tools, shutdown, task = fut.result(timeout=self.connect_timeout)
            except Exception as exc:  # 连接/初始化失败
                print(f"⚠ MCP server '{server.name}' 连接失败: {_format_connect_error(exc)}",
                      file=sys.stderr)
                continue
            self._sessions[server.name] = session
            self._tools[server.name] = tools
            self._shutdowns[server.name] = shutdown
            self._tasks[server.name] = task

    async def _connect(self, server: MCPServerConfig):
        """在 loop 内启动 _serve 长任务,等它进入上下文 + 初始化完成后返回 session/tools。"""
        ready: asyncio.Future = self.loop.create_future()
        shutdown = asyncio.Event()
        task = self.loop.create_task(self._serve(server, ready, shutdown))
        session, tools = await ready
        return session, tools, shutdown, task

    async def _serve(self, server: MCPServerConfig, ready: asyncio.Future,
                     shutdown: asyncio.Event) -> None:
        """长生命周期任务:进入上下文 → 初始化 → 交出 session → 阻塞等关闭 → 退出上下文。"""
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        try:
            server_args = _resolve_playwright_browser_args(server.args)
            command, command_args = _resolve_stdio_launch(server.command, server_args)
            env = _build_stdio_env(server)
            params = StdioServerParameters(
                command=command,
                args=command_args,
                env=env,
                cwd=_resolve_stdio_cwd(server.cwd),
            )
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
                tools = [
                    MCPToolInfo(
                        server=server.name,
                        name=t.name,
                        description=t.description or "",
                        # MCP SDK 1.x 使用 inputSchema；2.x 的 Python 属性改为
                        # input_schema（序列化 alias 仍可能是 inputSchema）。
                        input_schema=_mcp_tool_input_schema(t),
                    )
                    for t in listed.tools
                ]
                if not ready.done():
                    ready.set_result((session, tools))
                await shutdown.wait()  # 保持上下文存活,直到 stop()
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)

    # ── 工具发现 / 调用 ────────────────────────────────────────────────────────
    def tools(self) -> list[MCPToolInfo]:
        """返回所有已连接 server 的工具,带 server 归属。"""
        out: list[MCPToolInfo] = []
        for tool_list in self._tools.values():
            out.extend(tool_list)
        return out

    def call_tool(self, server_name: str, tool_name: str,
                  arguments: dict[str, Any], timeout: float = 60.0) -> tuple[str, bool]:
        """同步调用某 server 的工具,返回 (文本结果, 是否错误)。

        不抛异常:超时/异常都转成 (错误文本, True) 回给模型,让它自己决定下一步。
        """
        session = self._sessions.get(server_name)
        if session is None or self.loop is None:
            return (f"MCP server '{server_name}' not connected", True)
        coro = session.call_tool(tool_name, arguments or {})
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            result = fut.result(timeout=timeout)
        except FuturesTimeout:
            fut.cancel()
            return (f"timeout: MCP tool '{tool_name}' exceeded {timeout}s", True)
        except Exception as exc:
            return (format_exception(exc), True)
        return _content_to_text(result)

    # ── 关闭 ─────────────────────────────────────────────────────────────────
    def stop(self) -> None:
        """优雅关闭:置 shutdown 事件让 _serve 在自己 task 内退出上下文,再停 loop。"""
        if self.loop is None:
            return
        # 1. 通知每个 _serve 退出(必须在 loop 线程里 set)
        for shutdown in self._shutdowns.values():
            self.loop.call_soon_threadsafe(shutdown.set)
        # 2. 等 _serve task 跑完上下文退出
        for task in self._tasks.values():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._await_task(task), self.loop).result(timeout=10)
            except Exception:
                pass
        # 3. 停 loop + join 线程
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.loop = None
        self.thread = None
        self._sessions.clear()
        self._tools.clear()
        self._shutdowns.clear()
        self._tasks.clear()

    @staticmethod
    async def _await_task(task: asyncio.Task) -> None:
        try:
            await task
        except Exception:
            pass
