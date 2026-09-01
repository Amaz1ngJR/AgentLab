"""模型路由器 —— 根据配置选择 provider adapter，对外暴露统一接口。"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Optional

from app.config.schemas import LLMConfig
from app.models.provider_retry import ProviderCircuitBreaker, call_with_retry
from app.models.protocol import (
    ModelResponse,
    ProgressCallback,
    TextDeltaCallback,
    ThinkingDeltaCallback,
    ToolResult,
)


class ModelRouter:
    """统一的模型调用入口，内部持有具体 adapter。

    使用场景：程序入口调用 build_model_router(cfg) 得到此对象，
    传给 AgentSession 使用。
    """

    def __init__(self, adapter: Any):
        self._adapter = adapter
        configured_provider = getattr(getattr(adapter, "_cfg", None), "provider", "")
        self._configured_provider = configured_provider
        # Ollama 的模型 runner 与常驻 HTTP 服务是两个进程。冷启动、重新分配
        # 显存或 runner 短暂退出时，HTTP 服务仍在线，但 /v1 请求会连续返回
        # 502；默认的 2/4 秒两次重试经常早于 runner 恢复。只对本地 Ollama
        # 扩大恢复窗口，避免把云端 provider 的故障等待时间一并拉长。
        self._max_retries = 4 if configured_provider == "ollama" else 2
        self._max_retry_delay = 10.0 if configured_provider == "ollama" else 30.0
        self._breaker = ProviderCircuitBreaker(
            failure_threshold=self._max_retries + 1,
            recovery_seconds=10.0 if configured_provider == "ollama" else 30.0,
        )
        self._warmup_lock = threading.Lock()
        self._last_ollama_activity = 0.0

    def _warmup_ollama(self) -> None:
        """用原生空请求预加载 runner，避开 /v1 兼容接口冷启动 502。"""
        if self._configured_provider != "ollama":
            return
        cfg = getattr(self._adapter, "_cfg", None)
        base_url = getattr(cfg, "base_url", None)
        model = getattr(cfg, "model", None)
        if not base_url or not model:
            return

        # Ollama 默认 keep_alive 为 5 分钟；会话持续活跃时无需重复预热，空闲
        # 接近卸载窗口后再发一次空请求。锁防止同一 router 的并发首调用重复加载。
        with self._warmup_lock:
            now = time.monotonic()
            if now - self._last_ollama_activity < 240.0:
                return
            native_base = base_url.rstrip("/")
            if native_base.endswith("/v1"):
                native_base = native_base[:-3]
            body: dict[str, Any] = {
                "model": model,
                "stream": False,
                "keep_alive": "5m",
            }
            context_size = getattr(cfg, "context_size", None)
            if context_size:
                body["options"] = {"num_ctx": int(context_size)}
            request = urllib.request.Request(
                f"{native_base}/api/generate",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            timeout = float(getattr(cfg, "timeout_seconds", 120.0) or 120.0)

            def _preload() -> None:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    response.read()

            call_with_retry(
                _preload,
                breaker=self._breaker,
                max_retries=self._max_retries,
                max_delay=self._max_retry_delay,
            )
            self._last_ollama_activity = time.monotonic()

    def _record_activity(self) -> None:
        if self._configured_provider == "ollama":
            self._last_ollama_activity = time.monotonic()

    @property
    def model(self) -> str:
        return self._adapter.model

    @property
    def provider(self) -> str:
        return self._adapter.provider

    @property
    def profile_name(self) -> str | None:
        return getattr(getattr(self._adapter, "_cfg", None), "profile_name", None)

    @property
    def base_url(self) -> str | None:
        return getattr(getattr(self._adapter, "_cfg", None), "base_url", None)

    @property
    def context_size(self) -> int | None:
        return getattr(getattr(self._adapter, "_cfg", None), "context_size", None)

    @property
    def temperature(self) -> float | None:
        return getattr(getattr(self._adapter, "_cfg", None), "temperature", None)

    @property
    def reasoning_effort(self) -> str | None:
        return getattr(getattr(self._adapter, "_cfg", None), "reasoning_effort", None)

    @property
    def capabilities(self) -> list[str]:
        return list(getattr(getattr(self._adapter, "_cfg", None), "capabilities", []) or [])

    @property
    def supports_vision(self) -> bool:
        return "vision" in self.capabilities

    def chat(self, messages: list[dict], temperature: Optional[float] = None) -> str:
        self._warmup_ollama()
        result = self._adapter.chat(messages, temperature=temperature)
        self._record_activity()
        return result

    def create_message(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 4096,
        on_progress: Optional[ProgressCallback] = None,
        on_text_delta: Optional[TextDeltaCallback] = None,
        on_thinking_delta: Optional[ThinkingDeltaCallback] = None,
    ) -> ModelResponse:
        self._warmup_ollama()
        result = call_with_retry(
            lambda: self._adapter.create_message(
                messages,
                tools=tools,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                on_progress=on_progress,
                on_text_delta=on_text_delta,
                on_thinking_delta=on_thinking_delta,
            ),
            breaker=self._breaker,
            max_retries=self._max_retries,
            max_delay=self._max_retry_delay,
        )
        self._record_activity()
        return result

    def format_tool_results(self, results: list[ToolResult]) -> list[dict]:
        """把工具执行结果转成具体 provider 要求的下一轮输入项。

        Runtime 不关心格式细节,只需要把返回的 list 用 messages.extend 追加。
        三家 provider 的格式差异:
          - Anthropic: 一条 user 消息,content 是 tool_result block 列表
          - OpenAI Chat Completions: 每个工具一条 role=tool 消息
          - OpenAI Responses: 每个工具一条 type=function_call_output 项
        """
        return self._adapter.format_tool_results(results)

    def close(self) -> None:
        client = getattr(self._adapter, "_client", None)
        close = getattr(client, "close", None)
        if callable(close):
            close()


_PROVIDERS = {
    "anthropic":                "anthropic",
    "openai":                   "openai",
    "openai_compatible":        "openai_compatible",
    "ollama":                   "ollama",
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
    elif canonical == "openai":
        # OpenAI 官方 GPT 系列走 Responses API,与 Chat Completions 完全不同
        from app.models.openai_adapter import OpenAIAdapter
        adapter = OpenAIAdapter(cfg)
    elif canonical == "ollama":
        from app.models.ollama_adapter import OllamaAdapter
        adapter = OllamaAdapter(cfg)
    else:
        # Ollama / LM Studio / vLLM 等走 Chat Completions 协议子集
        from app.models.compatible_adapter import OpenAICompatibleAdapter
        adapter = OpenAICompatibleAdapter(cfg)

    return ModelRouter(adapter)
