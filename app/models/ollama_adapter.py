"""Ollama 原生 /api/chat 适配器。

不经过 OpenAI compatibility 层，避免 Windows 上模型 runner 冷启动/重载时
/v1/chat/completions 偶发连续 502。原生协议直接支持流式文本、thinking 和工具调用。
"""
from __future__ import annotations

import base64
import json
import uuid
from typing import Any, Optional

import requests

from app.config.schemas import LLMConfig
from app.models.compatible_adapter import (
    _extract_tool_calls_from_text,
    _normalize_chat_messages,
    _to_openai_tool,
)
from app.models.protocol import (
    ModelResponse,
    ProgressCallback,
    TextDeltaCallback,
    ThinkingDeltaCallback,
    ToolCall,
    ToolResult,
)
from app.models.stream_normalizer import StreamDeltaNormalizer
from app.models.token_progress import StreamingTokenProgress


class OllamaAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


def _native_base_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def _decode_data_url(url: str) -> str | None:
    if not url.startswith("data:") or "," not in url:
        return None
    header, encoded = url.split(",", 1)
    if ";base64" not in header:
        return None
    # 验证内容，避免把坏的 data URL 原样送入 Ollama。
    try:
        base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    return encoded


def _to_ollama_messages(messages: list[dict]) -> list[dict[str, Any]]:
    """把 OpenAI/Responses 历史清洗成 Ollama 原生 chat message。"""
    normalized = _normalize_chat_messages(messages)
    result: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}

    for message in normalized:
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            continue
        content = message.get("content", "")
        converted: dict[str, Any] = {"role": role, "content": ""}

        if isinstance(content, list):
            text_parts: list[str] = []
            images: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
                elif block.get("type") == "image_url":
                    image_url = block.get("image_url") or {}
                    encoded = _decode_data_url(str(image_url.get("url", "")))
                    if encoded:
                        images.append(encoded)
            converted["content"] = "".join(text_parts)
            if images:
                converted["images"] = images
        else:
            converted["content"] = "" if content is None else str(content)

        if role == "assistant" and message.get("tool_calls"):
            native_calls: list[dict[str, Any]] = []
            for index, raw_call in enumerate(message["tool_calls"]):
                fn = raw_call.get("function") or {}
                name = str(fn.get("name") or "")
                if not name:
                    continue
                arguments = fn.get("arguments") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                call_id = str(raw_call.get("id") or "")
                if call_id:
                    call_names[call_id] = name
                native_calls.append({
                    "type": "function",
                    "function": {
                        "index": index,
                        "name": name,
                        "arguments": arguments,
                    },
                })
            if native_calls:
                converted["tool_calls"] = native_calls

        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            tool_name = str(message.get("name") or call_names.get(call_id) or "")
            if tool_name:
                converted["tool_name"] = tool_name

        result.append(converted)
    return result


class OllamaAdapter:
    def __init__(self, cfg: LLMConfig):
        self._cfg = cfg
        self._client = requests.Session()
        self._chat_url = f"{_native_base_url(cfg.base_url or '')}/api/chat"
        self._tool_names_by_id: dict[str, str] = {}

    @property
    def model(self) -> str:
        return self._cfg.model

    @property
    def provider(self) -> str:
        return "ollama"

    def chat(self, messages: list[dict], temperature: Optional[float] = None) -> str:
        return self.create_message(messages, temperature=temperature).text

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
        all_messages = list(messages)
        if system and not any(m.get("role") == "system" for m in all_messages):
            all_messages = [{"role": "system", "content": system}] + all_messages

        options: dict[str, Any] = {
            "temperature": self._cfg.temperature if temperature is None else temperature,
            "num_predict": max_tokens,
        }
        if self._cfg.top_p is not None:
            options["top_p"] = self._cfg.top_p
        if self._cfg.context_size:
            options["num_ctx"] = int(self._cfg.context_size)

        body: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": _to_ollama_messages(all_messages),
            "stream": True,
            "think": bool(self._cfg.enable_thinking),
            "keep_alive": "5m",
            "options": options,
        }
        if tools:
            body["tools"] = [_to_openai_tool(tool) for tool in tools]

        progress = StreamingTokenProgress(on_progress, 0)
        normalizer = StreamDeltaNormalizer()
        progress.emit(force=True)
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        raw_calls: list[dict[str, Any]] = []
        seen_calls: set[str] = set()
        input_tokens = 0
        output_tokens = 0
        finish_reason: str | None = None
        actual_model: str | None = None

        timeout = (5.0, float(self._cfg.timeout_seconds))
        with self._client.post(
            self._chat_url,
            json=body,
            stream=True,
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                if isinstance(raw_line, bytes):
                    raw_line = raw_line.decode("utf-8")
                chunk = json.loads(raw_line)
                if chunk.get("error"):
                    raise OllamaAPIError(
                        int(chunk.get("status") or 500), str(chunk["error"]),
                    )
                actual_model = actual_model or chunk.get("model")
                message = chunk.get("message") or {}

                reasoning_delta = normalizer.normalize(
                    "reasoning", message.get("thinking"),
                )
                if reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                    if on_thinking_delta:
                        on_thinking_delta(reasoning_delta)
                    progress.add_reasoning(reasoning_delta)

                text_delta = normalizer.normalize("content", message.get("content"))
                if text_delta:
                    text_parts.append(text_delta)
                    if on_text_delta:
                        on_text_delta(text_delta)
                    progress.add_text(text_delta)

                for native_call in message.get("tool_calls") or []:
                    fn = native_call.get("function") or {}
                    name = str(fn.get("name") or "")
                    arguments = fn.get("arguments") or {}
                    if not name or not isinstance(arguments, dict):
                        continue
                    signature = json.dumps(
                        [name, arguments], ensure_ascii=False, sort_keys=True,
                    )
                    if signature not in seen_calls:
                        seen_calls.add(signature)
                        raw_calls.append({"name": name, "arguments": arguments})

                if chunk.get("done"):
                    finish_reason = chunk.get("done_reason")
                    input_tokens = int(chunk.get("prompt_eval_count") or 0)
                    output_tokens = int(chunk.get("eval_count") or 0)

        text = "".join(text_parts)
        tool_calls: list[ToolCall] = []
        payload_calls: list[dict[str, Any]] = []
        for raw_call in raw_calls:
            call_id = f"call_ollama_{uuid.uuid4().hex[:12]}"
            name = raw_call["name"]
            arguments = raw_call["arguments"]
            self._tool_names_by_id[call_id] = name
            tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
            payload_calls.append({
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            })

        # 与兼容适配器保持相同的小模型兜底：正文里的裸 JSON 工具调用也可恢复。
        if not tool_calls and tools and text.strip():
            recovered = _extract_tool_calls_from_text(text, {t["name"] for t in tools})
            for name, arguments in recovered:
                call_id = f"call_ollama_{uuid.uuid4().hex[:12]}"
                self._tool_names_by_id[call_id] = name
                tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
                payload_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                })
            if recovered:
                text = ""

        progress.set_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            force=True,
            final=True,
        )
        payload: dict[str, Any] = {"role": "assistant", "content": text}
        reasoning = "".join(reasoning_parts)
        if reasoning:
            payload["thinking"] = reasoning
        if payload_calls:
            payload["tool_calls"] = payload_calls
        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
            provider_payload=[payload],
            finish_reason=finish_reason,
            actual_model=actual_model,
            reasoning=reasoning,
        )

    def format_tool_results(self, results: list[ToolResult]) -> list[dict]:
        formatted: list[dict[str, Any]] = []
        for result in results:
            name = self._tool_names_by_id.get(result.tool_call_id, "")
            message: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": result.tool_call_id,
                "content": (
                    f"ERROR: {result.output}" if result.is_error else result.output
                ),
            }
            if name:
                # name 供历史清洗器恢复映射；实际发给 Ollama 时会转成 tool_name。
                message["name"] = name
                message["tool_name"] = name
            formatted.append(message)
        return formatted


__all__ = ["OllamaAdapter", "OllamaAPIError", "_to_ollama_messages"]
