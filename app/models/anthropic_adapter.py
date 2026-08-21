"""Anthropic Claude API adapter。

认证优先级: auth_token (Bearer) > api_key (x-api-key)。
auth_token 用于自建代理 / Claude Code 网关。
"""
from __future__ import annotations

from typing import Any, Optional

from app.config.schemas import LLMConfig
from app.attachments import materialize_image_block
from app.models.token_progress import StreamingTokenProgress
from app.models.protocol import (
    ModelResponse,
    ProgressCallback,
    TextDeltaCallback,
    ThinkingDeltaCallback,
    ToolCall,
    ToolResult,
)


def _materialize_anthropic_messages(messages: list[dict]) -> list[dict]:
    """把内部 file 图片引用转换为 Anthropic Messages 的 base64 source。"""
    converted: list[dict] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            converted.append(message)
            continue
        blocks: list[Any] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image":
                media_type, data = materialize_image_block(block)
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                })
            else:
                blocks.append(block)
        converted.append({**message, "content": blocks})
    return converted


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
        converted = _materialize_anthropic_messages(converted)
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
        on_thinking_delta: Optional[ThinkingDeltaCallback] = None,
    ) -> ModelResponse:
        system_from_msgs, converted = self._split_system(messages)
        converted = _materialize_anthropic_messages(converted)
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
        progress = StreamingTokenProgress(on_progress)
        progress.emit(force=True)

        with self._client.messages.stream(**params) as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                if etype == "message_start":
                    usage = getattr(event.message, "usage", None)
                    if usage:
                        in_tokens = getattr(usage, "input_tokens", 0) or 0
                        out_tokens = getattr(usage, "output_tokens", 0) or 0
                    progress.set_usage(
                        input_tokens=in_tokens,
                        output_tokens=out_tokens,
                        force=True,
                    )
                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if delta is not None and getattr(delta, "type", None) == "text_delta":
                        text_chunk = getattr(delta, "text", "") or ""
                        if text_chunk:
                            if on_text_delta:
                                on_text_delta(text_chunk)
                            progress.add_text(text_chunk)
                elif etype == "message_delta":
                    usage = getattr(event, "usage", None)
                    if usage:
                        v = getattr(usage, "output_tokens", None)
                        if v is not None:
                            out_tokens = v
                            progress.set_usage(output_tokens=v)

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
            progress.set_usage(
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                force=True,
                final=True,
            )

        # provider_payload 是 list[dict],由 Runtime 调 messages.extend(...) 追加。
        # Anthropic 要求把这一轮所有 content block(text + tool_use)合在
        # 同一条 assistant 消息里完整回放,否则下一轮 messages.create 会报
        # "tool_use without matching tool_result"。所以这里只产生一个 dict,
        # 但仍包成单元素 list 以匹配统一接口(OpenAI Responses 那边可能会有多条)。
        return ModelResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=usage,
            provider_payload=[{"role": "assistant", "content": raw_blocks}],
            finish_reason=getattr(message, "stop_reason", None),
            # API 实际使用的模型 ID。Anthropic 官方 API 会原样返回请求里写的
            # model;但代理(如 ai.vdian.net)有可能映射到别的真实模型,这里把
            # 它如实暴露给上层,Runtime / CLI 在不一致时可以警告
            actual_model=getattr(message, "model", None),
        )

    @staticmethod
    def format_tool_results(results: list[ToolResult]) -> list[dict]:
        """把工具执行结果转成 Anthropic 格式,作为 messages 的下一轮输入项返回。

        Anthropic Messages API 规定:
          - 工具结果必须放在 role=user 的消息里
          - content 是 tool_result 块列表,每个块的 tool_use_id 必须和上一轮
            assistant 消息里某个 tool_use 块的 id 对应
          - 一个 user 消息可以包含多个 tool_result 块(对应同一轮多个工具调用)

        所以即便有 N 个 ToolResult,也只产生一条 user 消息(里面 N 个 block)。
        """
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": r.tool_call_id,
                "content": r.output,
                "is_error": r.is_error,
            }
            for r in results
        ]
        return [{"role": "user", "content": blocks}]
