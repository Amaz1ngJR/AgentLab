"""MCP server 配置 —— 解析 config/mcp_servers.yaml。

定位:
  AgentLab 作为 MCP Client 连接用户配置的 MCP Server(见 technical_architecture.md §9)。
  本模块只负责"读哪些 server、怎么启动、默认是否启用、哪些工具免审批",
  实际连接与工具调用在 manager.py。

安全约定(§9.2):
  - 配置文件只保存环境变量名,不保存真实 token(本 MVP 只做 stdio,token 暂未用上)
  - stdio 命令以数组保存并直接启动,禁止经 shell 字符串展开
  - 新 server 默认 enabled=false,启用前 CLI 会展示 server 与工具
  - env_allowlist 限制透传给子进程的环境变量,默认只给 PATH,避免泄漏密钥
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


@dataclass
class MCPServerConfig:
    """一个 MCP Server 的配置记录。

    name          - server 标识,例如 "playwright"
    transport     - 目前只支持 "stdio"
    command       - stdio 启动可执行,例如 "npx"
    args          - 启动参数数组,例如 ["-y", "@playwright/mcp@latest", "--headless"]
    env_allowlist - 允许透传给子进程的环境变量名;默认只 PATH
    enabled       - 是否启用;新 server 默认 false
    risk          - server 级风险标签(用于展示与未来分级审批),默认 "browser_control"
    auto_approve  - 该 server 下免审批的工具名白名单(只读观察类),其余默认需审批
    """
    name: str
    transport: str = "stdio"
    command: Optional[str] = None
    args: list[str] = field(default_factory=list)
    env_allowlist: list[str] = field(default_factory=lambda: ["PATH"])
    enabled: bool = False
    risk: str = "browser_control"
    auto_approve: list[str] = field(default_factory=list)


def load_mcp_servers(path: Optional[Path] = None) -> list[MCPServerConfig]:
    """加载 MCP server 配置。

    优先级:显式 path > config/mcp_servers.yaml。文件不存在则返回空列表
    (向后兼容:没配过 MCP 的用户照常启动,只是没有 MCP 工具)。
    """
    cfg_path = path or (CONFIG_DIR / "mcp_servers.yaml")
    if not cfg_path.exists():
        return []
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    servers: list[MCPServerConfig] = []
    for name, body in (raw.get("servers") or {}).items():
        body = body or {}
        servers.append(MCPServerConfig(
            name=name,
            transport=body.get("transport", "stdio"),
            command=body.get("command"),
            args=list(body.get("args") or []),
            env_allowlist=list(body.get("env_allowlist") or ["PATH"]),
            enabled=bool(body.get("enabled", False)),
            risk=body.get("risk", "browser_control"),
            auto_approve=list(body.get("auto_approve") or []),
        ))
    return servers


def enabled_servers(path: Optional[Path] = None) -> list[MCPServerConfig]:
    """只返回 enabled=true 的 server。"""
    return [s for s in load_mcp_servers(path) if s.enabled]
