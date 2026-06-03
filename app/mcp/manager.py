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
  - 子进程 env 只透传 server.env_allowlist(默认 PATH),避免把密钥泄漏给 MCP server。
  - 工具输出经 redact() 脱敏后才回灌给(可能是云端的)模型。
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Any, Optional

from app.mcp.config import MCPServerConfig
from app.util.redact import format_exception, redact


@dataclass
class MCPToolInfo:
    """从 MCP server 发现的一个工具(已规整为内部表示)。"""
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]


def _content_to_text(result: Any) -> tuple[str, bool]:
    """把 MCP CallToolResult 转成 (文本, 是否错误)。

    content 是一组 block:text 直接取 .text;image/audio 只回占位说明(MVP 不回传
    二进制给模型);其它类型回 repr 兜底。输出整体过 redact()。
    is_error 取 result.isError(SDK 字段),取不到则按无错误处理。
    """
    is_error = bool(getattr(result, "isError", False))
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
                print(f"⚠ MCP server '{server.name}' 连接失败: {format_exception(exc)}",
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
            env = {k: os.environ[k] for k in server.env_allowlist if k in os.environ}
            params = StdioServerParameters(command=server.command, args=server.args, env=env)
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
                        input_schema=dict(t.inputSchema or {}),
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
