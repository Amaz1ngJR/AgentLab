"""OpenAI 原生 adapter —— 调用 OpenAI Responses API (推荐的新接口)。

为什么单独做一个 adapter:
  Responses API 不是 Chat Completions 的"超集",输入 / 输出 / 工具调用结构
  都跟 Chat 不同。把它塞进 OpenAICompatibleAdapter 会让那个 adapter 长出
  分支来回切,不如分开干净。

  OpenAICompatibleAdapter -> Chat Completions (Ollama / LM Studio / vLLM)
  OpenAIAdapter           -> Responses API (官方 GPT-5 / GPT-4.1 等)

关键结构差异(对比 Chat Completions):
  - 输入字段叫 input,不是 messages
  - 工具是扁平 dict: {type: "function", name, description, parameters}
    (Chat 是嵌套 {type: function, function: {name, ...}})
  - 模型上轮的"调用了哪些工具"用 type=function_call 项 + role=assistant
    message 项 *分开* 表示,不像 Chat 把 tool_calls 塞进 assistant message
  - 工具结果用 type=function_call_output 项,不是 role=tool 消息
  - 流式事件类型用 "response.output_text.delta" 等,跟 Chat 完全不同
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app.config.schemas import LLMConfig
from app.models.protocol import (
    ModelResponse,
    ProgressCallback,
    TextDeltaCallback,
    ThinkingDeltaCallback,
    ToolCall,
    ToolResult,
)


class OpenAIAdapter:
    """走 OpenAI Responses API 的 adapter,用于 GPT-5 等 OpenAI 原生模型。"""

    def __init__(self, cfg: LLMConfig):
        from openai import OpenAI

        self._cfg = cfg
        if not cfg.api_key:
            raise RuntimeError("openai provider 需要 OPENAI_API_KEY")

        kwargs: dict[str, Any] = {"timeout": cfg.timeout_seconds, "api_key": cfg.api_key}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self._client = OpenAI(**kwargs)

    @property
    def model(self) -> str:
        return self._cfg.model

    @property
    def provider(self) -> str:
        return "openai"

    def _base_params(self, messages: list[dict], temperature: Optional[float],
                     system: Optional[str]) -> dict[str, Any]:
        # Responses API 通过 instructions 顶级字段传 system,比塞进 input 更清晰
        # (语义和 Anthropic 的 system 参数一致)
        params: dict[str, Any] = {
            "model": self._cfg.model,
            "input": list(messages),
            "temperature": self._cfg.temperature if temperature is None else temperature,
        }
        if system:
            params["instructions"] = system
        if self._cfg.top_p is not None:
            params["top_p"] = self._cfg.top_p
        return params

    def chat(self, messages: list[dict], temperature: Optional[float] = None) -> str:
        """简单对话,不带工具。返回纯文本。"""
        params = self._base_params(messages, temperature, system=None)
        resp = self._client.responses.create(**params)
        # SDK 提供 output_text 便捷属性,自动拼接所有 text 段
        return getattr(resp, "output_text", "") or ""

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
        """带工具调用的完整对话,供 Agent 循环使用。"""
        params = self._base_params(messages, temperature, system)
        params["max_output_tokens"] = max_tokens
        if tools:
            # Responses API 的工具定义是扁平结构,直接 name / parameters,不嵌套 function
            params["tools"] = [_to_responses_tool(t) for t in tools]

        # ── 流式累积状态 ────────────────────────────────────────────────────
        # 输入 token 估算(在真实值到达前用,约 3 字符 / token)
        in_tokens_est = max(1, _estimate_input_tokens(messages, system))
        in_tokens = in_tokens_est
        out_tokens = 0
        out_chars = 0  # 用于估算 output_tokens

        def emit_progress() -> None:
            if on_progress:
                on_progress({"input_tokens": in_tokens, "output_tokens": out_tokens})

        emit_progress()

        # 流式事件中只处理文本增量(让 spinner 实时刷新);工具调用结果与最终
        # provider_payload 等待 get_final_response() 一次性拿全,避免分片重组的复杂度
        with self._client.responses.stream(**params) as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                if etype == "response.output_text.delta":
                    delta_text = getattr(event, "delta", "") or ""
                    if delta_text:
                        if on_text_delta:
                            on_text_delta(delta_text)
                        out_chars += len(delta_text)
                        est = max(out_tokens, out_chars // 3)
                        if est != out_tokens:
                            out_tokens = est
                            emit_progress()

            final = stream.get_final_response()

        # ── 从 final.output 解析最终结果 ────────────────────────────────────
        # output 是混合 item 列表:可能含 ResponseOutputMessage 与
        # ResponseFunctionToolCall。用 model_dump() 转 dict 方便后续 JSON 化与回放。
        output_items: list[dict] = []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for item in final.output:
            item_type = getattr(item, "type", None)
            item_dict = item.model_dump() if hasattr(item, "model_dump") else dict(item)
            output_items.append(item_dict)

            if item_type == "message":
                # message item 的 content 是一个列表,每项可能是 output_text / refusal
                for part in getattr(item, "content", []) or []:
                    if getattr(part, "type", None) == "output_text":
                        text_parts.append(getattr(part, "text", "") or "")
            elif item_type == "function_call":
                # 工具调用项:call_id 用于回传结果时对应,arguments 是 JSON 字符串
                call_id = getattr(item, "call_id", "")
                name = getattr(item, "name", "")
                args_raw = getattr(item, "arguments", "") or "{}"
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(id=call_id, name=name, arguments=args))

        # 真实 usage 覆盖估算值(若 SDK 提供)
        usage_obj = getattr(final, "usage", None)
        if usage_obj is not None:
            in_tokens = getattr(usage_obj, "input_tokens", in_tokens) or in_tokens
            out_tokens = getattr(usage_obj, "output_tokens", out_tokens) or out_tokens
            emit_progress()

        return ModelResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage={"input_tokens": in_tokens, "output_tokens": out_tokens},
            # provider_payload 是 list,Runtime 用 messages.extend 追加。
            # Responses API 把 message item 与 function_call item 拆开放,
            # 下一轮把这些 item 原样作为 input 一起发,模型才能理解上下文。
            provider_payload=output_items,
            finish_reason=getattr(final, "status", None),
            # 真实模型 ID:同 Anthropic 的 message.model,代理静默映射时这里
            # 会和请求时写的 model 不同,把它向上暴露
            actual_model=getattr(final, "model", None),
        )

    @staticmethod
    def format_tool_results(results: list[ToolResult]) -> list[dict]:
        """把工具执行结果转成 Responses API 的 function_call_output input items。

        Responses API 规定:
          - 每个工具结果是一条独立的 input item,type=function_call_output
          - call_id 必须对应上一轮 function_call item 的 call_id
          - output 是工具输出字符串(Responses API 不区分 is_error,错误信息
            直接写在 output 里加 ERROR 前缀让模型识别)
        """
        return [
            {
                "type": "function_call_output",
                "call_id": r.tool_call_id,
                "output": (f"ERROR: {r.output}" if r.is_error else r.output),
            }
            for r in results
        ]


def _to_responses_tool(t: dict) -> dict:
    """把 Anthropic 风格 input_schema 工具描述转成 Responses API 的扁平结构。

    Anthropic / 内部统一格式:
        {"name": ..., "description": ..., "input_schema": {...}}
    Responses API 期望:
        {"type": "function", "name": ..., "description": ..., "parameters": {...}}
    """
    return {
        "type": "function",
        "name": t["name"],
        "description": t.get("description", ""),
        "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
    }


def _estimate_input_tokens(messages: list[dict], system: Optional[str]) -> int:
    """粗略估算 input token 数:把消息里所有文本字段的字符数加起来 / 3。

    用于流式开始时让 spinner 显示一个非零的 input_tokens 占位值。真实数字
    由 final.usage 在末尾覆盖。中文 1 字 ≈ 0.6 token,英文 4 字符 ≈ 1 token,
    取折中 3 字符 ≈ 1 token。
    """
    char_count = 0
    if system:
        char_count += len(system)
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            char_count += len(content)
        elif isinstance(content, list):
            # content 可能是 part list,把 text 字段加起来
            for part in content:
                if isinstance(part, dict):
                    char_count += len(part.get("text", "") or "")
    return char_count // 3
