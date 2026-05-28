"""OpenAI-compatible adapter —— 覆盖 Ollama / LM Studio / vLLM 等本地端点。

P0 关键：实现 create_message() 使 Ollama 工具循环闭环。

OpenAI 工具循环与 Anthropic 的差异：
  - 工具调用在 response.choices[0].message.tool_calls 里（ChoiceDelta 对象列表）
  - provider_payload 存 OpenAI 格式的 assistant message dict，下轮原样放入 messages
  - 工具结果回传格式：{"role": "tool", "tool_call_id": ..., "content": ...}
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.config.schemas import LLMConfig
from app.models.protocol import (
    ModelResponse,
    ProgressCallback,
    TextDeltaCallback,
    ToolCall,
    ToolResult,
)


class OpenAICompatibleAdapter:
    def __init__(self, cfg: LLMConfig):
        from openai import OpenAI

        self._cfg = cfg
        self._client = OpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key or "ollama",
            timeout=cfg.timeout_seconds,
        )

    @property
    def model(self) -> str:
        return self._cfg.model

    @property
    def provider(self) -> str:
        return "openai_compatible"

    def _base_params(self, messages: list[dict], temperature: Optional[float]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": messages,
            "temperature": self._cfg.temperature if temperature is None else temperature,
        }
        if self._cfg.top_p is not None:
            params["top_p"] = self._cfg.top_p
        return params

    def chat(self, messages: list[dict], temperature: Optional[float] = None) -> str:
        resp = self._client.chat.completions.create(**self._base_params(messages, temperature))
        return resp.choices[0].message.content or ""

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
        # OpenAI-compatible 接口把 system 放在 messages 里（role=system）
        all_messages = list(messages)
        if system and not any(m.get("role") == "system" for m in all_messages):
            all_messages = [{"role": "system", "content": system}] + all_messages

        params = self._base_params(all_messages, temperature)
        params["max_tokens"] = max_tokens
        params["stream"] = True
        params["stream_options"] = {"include_usage": True}
        if tools:
            params["tools"] = [_to_openai_tool(t) for t in tools]
            params["tool_choice"] = "auto"

        # 输入 token 估算（在真值到达前用），约 3 字符一个 token
        in_tokens_est = max(1, sum(len(str(m.get("content", ""))) for m in all_messages) // 3)
        in_tokens = in_tokens_est
        out_tokens = 0
        out_chars = 0

        def emit_progress() -> None:
            if on_progress:
                on_progress({"input_tokens": in_tokens, "output_tokens": out_tokens})

        emit_progress()

        text_parts: list[str] = []
        # tool_calls 流式分片到达：index -> {"id", "name", "args_str"}
        tool_calls_buf: dict[int, dict[str, str]] = {}
        finish_reason: Optional[str] = None

        stream = self._client.chat.completions.create(**params)
        for chunk in stream:
            if chunk.choices:
                choice = chunk.choices[0]
                delta = choice.delta
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                if delta is not None:
                    if getattr(delta, "content", None):
                        chunk_text = delta.content
                        text_parts.append(chunk_text)
                        if on_text_delta:
                            on_text_delta(chunk_text)
                        out_chars += len(chunk_text)
                        est = max(out_tokens, out_chars // 3)
                        if est != out_tokens:
                            out_tokens = est
                            emit_progress()
                    if getattr(delta, "tool_calls", None):
                        for tc in delta.tool_calls:
                            idx = getattr(tc, "index", 0) or 0
                            buf = tool_calls_buf.setdefault(idx, {"id": "", "name": "", "args": ""})
                            if getattr(tc, "id", None):
                                buf["id"] = tc.id
                            fn = getattr(tc, "function", None)
                            if fn is not None:
                                if getattr(fn, "name", None):
                                    buf["name"] = fn.name
                                if getattr(fn, "arguments", None):
                                    buf["args"] += fn.arguments

            usage = getattr(chunk, "usage", None)
            if usage:
                p = getattr(usage, "prompt_tokens", None)
                c = getattr(usage, "completion_tokens", None)
                if p is not None:
                    in_tokens = p
                if c is not None:
                    out_tokens = c
                emit_progress()

        text = "".join(text_parts)

        # 拼装 tool_calls
        tool_calls: list[ToolCall] = []
        raw_tool_calls: list[dict] = []
        for idx in sorted(tool_calls_buf):
            buf = tool_calls_buf[idx]
            if not buf["name"]:
                continue
            try:
                args = json.loads(buf["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=buf["id"], name=buf["name"], arguments=args))
            raw_tool_calls.append({
                "id": buf["id"],
                "type": "function",
                "function": {"name": buf["name"], "arguments": buf["args"] or "{}"},
            })

        payload: dict[str, Any] = {"role": "assistant", "content": text}
        if raw_tool_calls:
            payload["tool_calls"] = raw_tool_calls

        usage_dict = {"input_tokens": in_tokens, "output_tokens": out_tokens}

        # provider_payload 是 list[dict],由 Runtime 调 messages.extend 追加。
        # OpenAI Chat Completions 用单条 assistant 消息表示这一轮(无论有多少
        # 工具调用都塞在 message.tool_calls 里),所以这里只产生一个 dict,
        # 包成单元素 list 以匹配统一接口。
        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            usage=usage_dict,
            provider_payload=[payload],
            finish_reason=finish_reason,
        )

    @staticmethod
    def format_tool_results(results: list[ToolResult]) -> list[dict]:
        """把工具执行结果转成 OpenAI Chat Completions 格式。

        Chat Completions 规定:
          - 每个工具结果是一条独立 role=tool 的消息
          - tool_call_id 必须对应上一轮 assistant.tool_calls[*].id
          - content 是工具输出字符串(无 is_error 字段,出错信息直接写在 content 里)

        所以 N 个 ToolResult 会产生 N 条独立 message;is_error 通过在 content
        里加 "ERROR: " 前缀让模型识别(Chat Completions 协议本身不区分)。
        """
        return [
            {
                "role": "tool",
                "tool_call_id": r.tool_call_id,
                "content": (f"ERROR: {r.output}" if r.is_error else r.output),
            }
            for r in results
        ]


def _to_openai_tool(t: dict) -> dict:
    """把 Anthropic 风格的工具 schema 转成 OpenAI function tool 格式。"""
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
        },
    }
