"""MCP 工具适配器 —— 把发现的 MCP 工具包装成 ToolDescriptor。

设计要点(technical_architecture.md §9.2):
  - 每个 MCP 工具映射成统一的 ToolDescriptor,继承 server risk/origin/host。
  - 同名工具不覆盖内置:内置 read_file/write_file/list_dir/code_search/shell/todo_write
    是基础能力,MCP 不能顶替(§ "待接入 MCP 清单" 原则)。同名则跳过并警告。
  - 审批:MCP 工具默认 requires_approval=True(动作经浏览器/外部进程执行,风险高);
    只有落在 server.auto_approve 白名单里的只读观察类工具(如 browser_snapshot)才免审批。
"""
from __future__ import annotations

import re
import sys
from typing import Optional

from app.mcp.config import MCPServerConfig
from app.mcp.manager import MCPManager, MCPToolInfo
from app.tools.registry import Tool, ToolExecutionError

# 调用 MCP 工具的默认超时(秒)。浏览器导航/等待可能偏慢,给得比本地工具宽。
DEFAULT_CALL_TIMEOUT = 60.0


def _normalize_mcp_args(tool_name: str, args: dict) -> dict:
    """规范化本地模型常生成的 Playwright ref 包装格式。"""
    normalized = dict(args or {})
    if not tool_name.startswith("browser_"):
        return normalized
    for key in ("target", "ref"):
        value = normalized.get(key)
        if not isinstance(value, str):
            continue
        match = re.fullmatch(r"\s*\[ref=([^\]]+)]\s*", value)
        if match:
            normalized[key] = match.group(1).strip()
    if tool_name == "browser_snapshot":
        target = normalized.get("target")
        if isinstance(target, str) and target.strip().lower() in {
            "page", "root", "document", "body", "html", "current-page", "whole-page",
        }:
            # 全页快照的正确调用是省略 target；page/root 并不是 DOM ref。
            normalized.pop("target", None)
            # 本地模型经常同时虚构一个临时文件名。filename 会让 MCP 只保存文件、
            # 不把快照正文返回给模型，因此在这种全页误调用中一并移除。
            normalized.pop("filename", None)
    return normalized


def _make_executor(manager: MCPManager, server: str, tool_name: str):
    """生成同步 executor 闭包:把 (args) 转发给 manager.call_tool。"""
    def _execute(args: dict) -> str:
        wire_args = _normalize_mcp_args(tool_name, args)
        text, is_error = manager.call_tool(
            server, tool_name, wire_args, timeout=DEFAULT_CALL_TIMEOUT,
        )
        # MCP 协议错误抛成预期工具错误,Registry 会把 is_error 标为 True,
        # 同时将消息回灌模型并写入统一审计。
        if is_error:
            raise ToolExecutionError(f"[mcp error] {text}")
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
    config_by_server = {s.name: s for s in server_configs}

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
        server = config_by_server.get(info.server)
        risk = server.risk if server is not None else "destructive"
        is_browser = info.name.startswith("browser_")
        is_observation = any(
            token in info.name
            for token in ("snapshot", "screenshot", "console_messages", "network_requests")
        )
        tools.append(Tool(
            name=info.name,
            description=(
                f"[MCP server: {info.server}] "
                f"{info.description or info.name}"
                + (
                    " For a full-page snapshot call with an empty object {}. "
                    "Do not invent target='[ref=page]' or filename; target is only "
                    "for a real element ref returned by an earlier snapshot."
                    if info.name == "browser_snapshot" else ""
                )
            ),
            input_schema=info.input_schema or {"type": "object", "properties": {}},
            executor=_make_executor(manager, info.server, info.name),
            risk=risk,
            target_type="browser" if is_browser else "mcp_server",
            scope=f"mcp_server:{info.server}",
            origin="mcp",
            host=info.server,
            requires_observation=is_browser and not is_observation,
            requires_approval=not auto,  # 白名单内免审批,其余需审批
        ))
    return tools
