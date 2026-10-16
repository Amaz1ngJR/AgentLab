"""模型路由器 —— 根据配置选择 provider adapter，对外暴露统一接口。"""
from __future__ import annotations

import time
import copy
import contextvars
import queue
import threading
from typing import Any, Callable, Optional

from app.agent.cancel import CancelToken
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

    def __init__(
        self,
        adapter: Any,
        *,
        retry_sleep: Callable[[float], None] = time.sleep,
    ):
        self._adapter = adapter
        self._breaker = ProviderCircuitBreaker()
        self._retry_sleep = retry_sleep

    def fork(self) -> "ModelRouter":
        """Independent SDK client and retry breaker for concurrent child runs."""
        cfg = getattr(self._adapter, "_cfg", None)
        if cfg is None:
            raise ValueError("provider does not support isolated child clients")
        return build_model_router(copy.deepcopy(cfg))

    @property
    def model(self) -> str:
        return self._adapter.model

    @property
    def provider(self) -> str:
        return self._adapter.provider

    @property
    def temperature(self) -> float | None:
        return getattr(getattr(self._adapter, "_cfg", None), "temperature", None)

    @property
    def reasoning_effort(self) -> str | None:
        return getattr(getattr(self._adapter, "_cfg", None), "reasoning_effort", None)

    @property
    def top_p(self) -> float | None:
        return getattr(getattr(self._adapter, "_cfg", None), "top_p", None)

    @property
    def profile_name(self) -> str | None:
        return getattr(getattr(self._adapter, "_cfg", None), "profile_name", None)

    @property
    def context_size(self) -> int | None:
        return getattr(getattr(self._adapter, "_cfg", None), "context_size", None)

    @property
    def capabilities(self) -> list[str]:
        return list(getattr(getattr(self._adapter, "_cfg", None), "capabilities", []) or [])

    @property
    def supports_vision(self) -> bool:
        return "vision" in self.capabilities

    def chat(self, messages: list[dict], temperature: Optional[float] = None) -> str:
        return self._adapter.chat(messages, temperature=temperature)

    def create_message(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        on_progress: Optional[ProgressCallback] = None,
        on_text_delta: Optional[TextDeltaCallback] = None,
        on_thinking_delta: Optional[ThinkingDeltaCallback] = None,
        cancel: Optional[CancelToken] = None,
        discard_partial: Optional[Callable[[BaseException], None]] = None,
        on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
        retry_until_cancelled: bool = False,
    ) -> ModelResponse:
        adapter_kwargs: dict[str, Any] = {
            "tools": tools,
            "system": system,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "on_progress": on_progress,
            "on_text_delta": on_text_delta,
            "on_thinking_delta": on_thinking_delta,
        }
        if cancel is not None and _accepts_keyword(self._adapter.create_message, "cancel"):
            adapter_kwargs["cancel"] = cancel
        return call_with_retry(
            lambda: self._adapter.create_message(
                messages,
                **adapter_kwargs,
            ),
            on_retry=on_retry,
            cancel=cancel,
            breaker=self._breaker,
            discard_partial=discard_partial,
            sleep=self._retry_sleep,
            max_retries=None if retry_until_cancelled else 2,
        )

    def format_tool_results(self, results: list[ToolResult]) -> list[dict]:
        """把工具执行结果转成具体 provider 要求的下一轮输入项。

        Runtime 不关心格式细节,只需要把返回的 list 用 messages.extend 追加。
        三家 provider 的格式差异:
          - Anthropic: 一条 user 消息,content 是 tool_result block 列表
          - OpenAI Chat Completions: 每个工具一条 role=tool 消息
          - OpenAI Responses: 每个工具一条 type=function_call_output 项
        """
        return self._adapter.format_tool_results(results)


_PROVIDERS = {
    "anthropic":                "anthropic",
    "openai":                   "openai",
    "openai_compatible":        "openai_compatible",
    "ollama":                   "openai_compatible",
    "lmstudio":                 "openai_compatible",
    "vllm":                     "openai_compatible",
    "remote_openai_compatible": "openai_compatible",
}


def call_create_message(
    target: Any,
    messages: list[dict],
    *,
    tools: Optional[list[dict]] = None,
    system: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    on_progress: Optional[ProgressCallback] = None,
    on_text_delta: Optional[TextDeltaCallback] = None,
    on_thinking_delta: Optional[ThinkingDeltaCallback] = None,
    cancel: Optional[CancelToken] = None,
    discard_partial: Optional[Callable[[BaseException], None]] = None,
    on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
    retry_until_cancelled: bool = False,
) -> ModelResponse:
    """转发 create_message，只在目标接受时才传可选关键字参数。

    重试相关参数只传给支持它们的 Router。测试替身和第三方实现可以继续使用旧签名。
    """
    kwargs: dict[str, Any] = {
        "tools": tools,
        "system": system,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "on_progress": on_progress,
        "on_text_delta": on_text_delta,
        "on_thinking_delta": on_thinking_delta,
    }
    method = getattr(target, "create_message")
    optional = {
        "cancel": cancel,
        "discard_partial": discard_partial,
        "on_retry": on_retry,
        "retry_until_cancelled": retry_until_cancelled if retry_until_cancelled else None,
    }
    for name, value in optional.items():
        if value is not None and _accepts_keyword(method, name):
            kwargs[name] = value
    if cancel is None:
        return method(messages, **kwargs)
    cancel.raise_if_cancelled()
    # Isolate a slow SDK call from session state. A cancelled request may finish
    # later, but its response and callbacks must never enter the next turn.
    snapshot = copy.deepcopy(messages)
    kwargs["tools"] = copy.deepcopy(tools)
    mailbox: queue.Queue = queue.Queue()
    callbacks = {}
    for name in ("on_progress", "on_text_delta", "on_thinking_delta", "on_retry", "discard_partial"):
        callback = kwargs.get(name)
        if callback is not None:
            callbacks[name] = callback

            def enqueue(*args, _name=name):
                if not cancel.cancelled:
                    mailbox.put(("callback", (_name, args)))
            kwargs[name] = enqueue

    def invoke() -> None:
        try:
            mailbox.put(("result", method(snapshot, **kwargs)))
        except BaseException as exc:
            mailbox.put(("error", exc))

    context = contextvars.copy_context()
    threading.Thread(
        target=lambda: context.run(invoke), name="agentlab-model-request", daemon=True,
    ).start()
    while True:
        cancel.raise_if_cancelled()
        try:
            kind, value = mailbox.get(timeout=0.02)
        except queue.Empty:
            continue
        cancel.raise_if_cancelled()
        if kind == "callback":
            name, args = value
            try:
                callbacks[name](*args)
            except BaseException:
                cancel.cancel()
                raise
        elif kind == "error":
            raise value
        else:
            return value


def _accepts_keyword(func: Any, name: str) -> bool:
    """判断可调用对象是否接受指定关键字参数（含 **kwargs）。"""
    import inspect

    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return True  # 内建/无法内省：按接受处理，让调用方自己报错更直观
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == name and param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


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
    else:
        # Ollama / LM Studio / vLLM 等走 Chat Completions 协议子集
        from app.models.compatible_adapter import OpenAICompatibleAdapter
        adapter = OpenAICompatibleAdapter(cfg)

    return ModelRouter(adapter)
