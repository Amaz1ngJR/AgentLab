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
    skills          - 显式启用的 Skill 名单（skill_id），注入工作流上下文；
                      仅影响上下文，不授予工具权限（见 app/skills）
    memory_policy   - 记忆策略：none/read/read_write（默认 none）
    max_steps       - 一次 run 的模型往返总上限（兼容旧配置名，默认 8）
    max_task_steps  - 单个子任务的模型往返上限（默认 min(8, max_steps)）
    orchestrate     - 是否启用编排能力（False 强制 Direct，默认 True）
    mode            - auto/direct/task；未设置时兼容 orchestrate 的旧语义
    """
    agent_id: str
    name: str
    model_profile: str
    system_prompt: Optional[str] = None
    tools: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    memory_policy: str = "none"   # none | read | read_write
    max_steps: int = 8
    max_task_steps: int | None = None
    orchestrate: bool = True
    mode: str = "legacy"

    def __post_init__(self) -> None:
        if self.mode not in {"legacy", "auto", "direct", "task"}:
            raise ValueError(f"mode 必须是 legacy/auto/direct/task，当前值: {self.mode!r}")
        if self.mode == "auto" and not self.orchestrate:
            raise ValueError("mode=auto 需要 orchestrate=True")
        if self.max_steps <= 0:
            raise ValueError("AgentProfile.max_steps 必须 > 0")
        if self.max_task_steps is not None and self.max_task_steps <= 0:
            raise ValueError("AgentProfile.max_task_steps 必须 > 0")


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
            skills=list(body.get("skills") or []),
            memory_policy=body.get("memory_policy", "none"),
            max_steps=int(body.get("max_steps", 8)),
            max_task_steps=(
                int(body["max_task_steps"])
                if body.get("max_task_steps") is not None else None
            ),
            orchestrate=_parse_bool(body.get("orchestrate"), default=True),
            mode=_parse_mode(body.get("mode")),
        )
    return profiles


def _parse_mode(value) -> str:
    """解析 profile 模式；legacy 表示沿用 orchestrate 布尔开关。"""
    if value is None:
        return "legacy"
    if not isinstance(value, str) or value.strip().lower() not in {"auto", "direct", "task"}:
        raise ValueError(f"mode 必须是 auto/direct/task，当前值: {value!r}")
    return value.strip().lower()


def _parse_bool(value, *, default: bool) -> bool:
    """严格解析 YAML/字符串布尔值，避免 bool("false") 反而为 True。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"orchestrate 必须是布尔值，当前值: {value!r}")
