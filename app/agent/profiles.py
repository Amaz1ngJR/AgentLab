"""AgentProfile —— 一个 Agent 实例的静态配置。

职责：定义"这个 Agent 是谁、用什么模型、有哪些工具/MCP、记忆策略是什么"。
运行时由 SessionRouter 根据 profile 构建 AgentSession。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


@dataclass
class AgentProfile:
    """一个 Agent 的静态配置记录。

    agent_id        - 唯一标识，用于 /session new <agent_id>
    name            - 对用户展示的名称
    model_profile   - 使用 config/models.yaml 中哪个模型 profile
    system_prompt   - 覆盖默认 system prompt（None 则用全局默认）
    tools           - 明确允许的内置工具名单（空列表 = 使用全部内置工具）
    mcp_servers     - 允许加载的 MCP server 名单（空列表 = 继承全局配置）
    memory_policy   - 记忆策略：none/read/read_write（默认 none）
    max_steps       - 最大工具循环步数（默认 8）
    """
    agent_id: str
    name: str
    model_profile: str
    system_prompt: Optional[str] = None
    tools: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    memory_policy: str = "none"   # none | read | read_write
    max_steps: int = 8


def load_agent_profiles(path: Optional[Path] = None) -> dict[str, AgentProfile]:
    """加载 config/agents.yaml 中的 Agent profile 列表。

    文件不存在时返回空字典（向后兼容，不强制要求配置该文件）。
    """
    cfg_path = path or (CONFIG_DIR / "agents.yaml")
    if not cfg_path.exists():
        return {}
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    profiles: dict[str, AgentProfile] = {}
    for agent_id, body in (raw.get("agents") or {}).items():
        body = body or {}
        profiles[agent_id] = AgentProfile(
            agent_id=agent_id,
            name=body.get("name", agent_id),
            model_profile=body.get("model_profile", ""),
            system_prompt=body.get("system_prompt"),
            tools=list(body.get("tools") or []),
            mcp_servers=list(body.get("mcp_servers") or []),
            memory_policy=body.get("memory_policy", "none"),
            max_steps=int(body.get("max_steps", 8)),
        )
    return profiles
