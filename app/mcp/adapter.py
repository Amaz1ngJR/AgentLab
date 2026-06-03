"""MCP 工具适配器 —— 把发现的 MCP 工具包装成内置 Tool,接入现有 ToolRegistry。

设计要点(technical_architecture.md §9.2):
  - 每个 MCP 工具映射成统一的 ToolDescriptor(这里复用现有 Tool dataclass)。
  - 同名工具不覆盖内置:内置 read_file/write_file/list_dir/code_search/shell/todo_write
    是基础能力,MCP 不能顶替(§ "待接入 MCP 清单" 原则)。同名则跳过并警告。
  - 审批:MCP 工具默认 requires_approval=True(动作经浏览器/外部进程执行,风险高);
    只有落在 server.auto_approve 白名单里的只读观察类工具(如 browser_snapshot)才免审批。
"""
from __future__ import annotations

import sys
from typing import Optional

from app.mcp.config import MCPServerConfig
from app.mcp.manager import MCPManager, MCPToolInfo
from app.tools.registry import Tool

# 调用 MCP 工具的默认超时(秒)。浏览器导航/等待可能偏慢,给得比本地工具宽。
DEFAULT_CALL_TIMEOUT = 60.0


def _make_executor(manager: MCPManager, server: str, tool_name: str):
    """生成同步 executor 闭包:把 (args) 转发给 manager.call_tool。"""
    def _execute(args: dict) -> str:
        text, is_error = manager.call_tool(server, tool_name, args, timeout=DEFAULT_CALL_TIMEOUT)
        # registry.execute 用 (text, is_error) 中的 text;is_error 通过抛异常体现。
        # 这里 MCP 错误已是文本,直接把错误信息当结果返回(前缀标明),
        # 让模型看到失败原因而不是中断循环。
        if is_error:
            return f"[mcp error] {text}"
        return text
    return _execute


def build_mcp_tools(
    manager: MCPManager,
    server_configs: list[MCPServerConfig],
    reserved_names: Optional[set[str]] = None,
) -> list[Tool]:
    """把 manager 已发现的 MCP 工具转成 Tool 列表。

    reserved_names: 已被内置工具占用的名字,MCP 同名工具会被跳过(不覆盖内置)。
    """
    reserved = reserved_names or set()
    # server name -> auto_approve 白名单,用于决定每个工具是否免审批
    auto_by_server = {s.name: set(s.auto_approve) for s in server_configs}

    tools: list[Tool] = []
    seen: set[str] = set()
    for info in manager.tools():
        if info.name in reserved:
            print(f"⚠ MCP 工具 '{info.name}'(server={info.server})与内置工具同名,"
                  f"已跳过(内置工具不被 MCP 覆盖)", file=sys.stderr)
            continue
        if info.name in seen:
            print(f"⚠ MCP 工具 '{info.name}' 在多个 server 重名,已跳过后出现的那个",
                  file=sys.stderr)
            continue
        seen.add(info.name)

        auto = info.name in auto_by_server.get(info.server, set())
        tools.append(Tool(
            name=info.name,
            description=info.description or f"(MCP tool from {info.server})",
            input_schema=info.input_schema or {"type": "object", "properties": {}},
            executor=_make_executor(manager, info.server, info.name),
            requires_approval=not auto,  # 白名单内免审批,其余需审批
        ))
    return tools
