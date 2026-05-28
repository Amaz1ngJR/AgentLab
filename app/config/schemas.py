"""配置数据结构 —— LLMConfig dataclass。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ModelProfile:
    """config/models.yaml 中一条 profile 记录（新格式）。"""
    name: str
    provider: str
    model: str
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    auth_token_env: Optional[str] = None
    base_url_env: Optional[str] = None
    capabilities: list[str] = None
    params: dict[str, Any] = None

    def __post_init__(self):
        if self.capabilities is None:
            self.capabilities = []
        if self.params is None:
            self.params = {}


@dataclass
class LLMConfig:
    """运行时使用的完整配置，由 load_config() 生成。"""
    provider: str
    model: str
    base_url: Optional[str]
    api_key: Optional[str]
    auth_token: Optional[str]
    temperature: float
    top_p: Optional[float]
    context_size: Optional[int]
    timeout_seconds: float
    stream: bool
    profile_name: Optional[str] = None   # 激活的 profile 名称（如 "cloud_claude"）
    capabilities: list[str] = field(default_factory=list)  # 例 ["chat", "tools", "streaming"]
