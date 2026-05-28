"""Anthropic Claude API adapter。

认证优先级: auth_token (Bearer) > api_key (x-api-key)。
auth_token 用于自建代理 / Claude Code 网关。
"""
from __future__ import annotations

from typing import Any, Optional

from app.config.schemas import LLMConfig
from app.models.protocol import (
    ModelResponse,
    ProgressCallback,
    TextDeltaCallback,
    ToolCall,
)


class AnthropicAdapter:
    def __init__(self, cfg: LLMConfig):
        from anthropic import Anthropic

        if not (cfg.auth_token or cfg.api_key):
            raise RuntimeError("anthropic provider 需要 ANTHROPIC_AUTH_TOKEN 或 ANTHROPIC_API_KEY")

        kwargs: dict[str, Any] = {"timeout": cfg.timeout_seconds}
        if cfg.auth_token:
            kwargs["auth_token"] = cfg.auth_token
        else:
            kwargs["api_key"] = cfg.api_key
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self._client = Anthropic(**kwargs)
        self._cfg = cfg

    @property
    def model(self) -> str:
        return self._cfg.model

    @property
    def provider(self) -> str:
        return "anthropic"

    @staticmethod
    def _split_system(messages: list[dict]) -> tuple[Optional[str], list[dict]]:
        """Anthropic API 要求 system 作为顶级参数，不能混在 messages 里。"""
        parts, rest = [], []
        for msg in messages:
            if msg.get("role") == "system":
                if msg.get("content"):
                    parts.append(msg["content"])
            else:
                rest.append(msg)
        return ("\n\n".join(parts) if parts else None), rest

    def chat(self, messages: list[dict], temperature: Optional[float] = None) -> str:
        system_text, converted = self._split_system(messages)
        params: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": converted,
            "temperature": self._cfg.temperature if temperature is None else temperature,
            "max_tokens": 4096,
        }
        if system_text:
            params["system"] = system_text
        if self._cfg.top_p is not None:
            params["top_p"] = self._cfg.top_p
        msg = self._client.messages.create(**params)
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

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
        system_from_msgs, converted = self._split_system(messages)
        system_text = system or system_from_msgs

        params: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": converted,
            "temperature": self._cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens,
        }
        if system_text:
            params["system"] = system_text
        if self._cfg.top_p is not None:
            params["top_p"] = self._cfg.top_p
        if tools:
            params["tools"] = tools

        in_tokens = 0
        out_tokens = 0
        out_chars = 0  # 字符累计，用于在代理只在末尾返回 usage 时估算 token

        def emit_progress() -> None:
            if on_progress:
                on_progress({"input_tokens": in_tokens, "output_tokens": out_tokens})

        with self._client.messages.stream(**params) as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                if etype == "message_start":
                    usage = getattr(event.message, "usage", None)
                    if usage:
                        in_tokens = getattr(usage, "input_tokens", 0) or 0
                        out_tokens = getattr(usage, "output_tokens", 0) or 0
                    emit_progress()
                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if delta is not None and getattr(delta, "type", None) == "text_delta":
                        text_chunk = getattr(delta, "text", "") or ""
                        if text_chunk:
                            if on_text_delta:
                                on_text_delta(text_chunk)
                            out_chars += len(text_chunk)
                            est = max(out_tokens, out_chars // 3)
                            if est != out_tokens:
                                out_tokens = est
                                emit_progress()
                elif etype == "message_delta":
                    usage = getattr(event, "usage", None)
                    if usage:
                        v = getattr(usage, "output_tokens", None)
                        if v is not None and v > out_tokens:
                            out_tokens = v
                            emit_progress()

            message = stream.get_final_message()

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw_blocks: list[dict] = []
        for block in message.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
                raw_blocks.append({"type": "text", "text": block.text})
            elif btype == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=dict(block.input or {}),
                ))
                raw_blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input or {}),
                })

        usage: dict[str, int] = {}
        if getattr(message, "usage", None):
            usage = {
                "input_tokens":  getattr(message.usage, "input_tokens",  in_tokens) or in_tokens,
                "output_tokens": getattr(message.usage, "output_tokens", out_tokens) or out_tokens,
            }
        else:
            usage = {"input_tokens": in_tokens, "output_tokens": out_tokens}

        # 用最终 usage 再发一次进度，确保 spinner 显示的数字与 [stats] 一致
        if on_progress:
            on_progress(dict(usage))

        return ModelResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=usage,
            provider_payload=raw_blocks,
            finish_reason=getattr(message, "stop_reason", None),
        )
