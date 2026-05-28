"""模型路由器 —— 根据配置选择 provider adapter，对外暴露统一接口。"""
from __future__ import annotations

from typing import Any, Optional

from app.config.schemas import LLMConfig
from app.models.protocol import ModelResponse, ProgressCallback, TextDeltaCallback


class ModelRouter:
    """统一的模型调用入口，内部持有具体 adapter。

    使用场景：程序入口调用 build_model_router(cfg) 得到此对象，
    传给 AgentSession 使用。
    """

    def __init__(self, adapter: Any):
        self._adapter = adapter

    @property
    def model(self) -> str:
        return self._adapter.model

    @property
    def provider(self) -> str:
        return self._adapter.provider

    def chat(self, messages: list[dict], temperature: Optional[float] = None) -> str:
        return self._adapter.chat(messages, temperature=temperature)

    def create_message(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 4096,
        on_progress: Optional[ProgressCallback] = None,
        on_text_delta: Optional[TextDeltaCallback] = None,
    ) -> ModelResponse:
        return self._adapter.create_message(
            messages,
            tools=tools,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            on_progress=on_progress,
            on_text_delta=on_text_delta,
        )


_PROVIDERS = {
    "anthropic":                "anthropic",
    "openai_compatible":        "openai_compatible",
    "ollama":                   "openai_compatible",
    "lmstudio":                 "openai_compatible",
    "vllm":                     "openai_compatible",
    "remote_openai_compatible": "openai_compatible",
}


def build_model_router(cfg: Optional[LLMConfig] = None) -> ModelRouter:
    """根据配置创建 ModelRouter。cfg 为 None 时自动调用 load_config()。"""
    if cfg is None:
        from app.config.loader import load_config
        cfg = load_config()

    canonical = _PROVIDERS.get(cfg.provider)
    if canonical is None:
        raise ValueError(f"未知的 provider='{cfg.provider}'，支持: {sorted(_PROVIDERS)}")

    if canonical == "anthropic":
        from app.models.anthropic_adapter import AnthropicAdapter
        adapter = AnthropicAdapter(cfg)
    else:
        from app.models.compatible_adapter import OpenAICompatibleAdapter
        adapter = OpenAICompatibleAdapter(cfg)

    return ModelRouter(adapter)
